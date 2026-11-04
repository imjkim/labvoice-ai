"""Search over the lab's standard operating procedures (SOPs): one chunk per numbered section.

Retrieval is hybrid:
- BM25 over words (pure Python, no dependencies). Strong on exact lab vocabulary: "HER2",
  "IHC-01", "4 micrometers", "SOP-420".
- Embeddings from any OpenAI-compatible /embeddings endpoint (Ollama by default). Strong on
  paraphrase: "the stainer flunked its morning check" -> "Steps after a failed QC".
- Reciprocal rank fusion (RRF) merges the two rankings without having to calibrate their scores.
If the embedding endpoint isn't reachable, search falls back to BM25 and says so.

Each chunk is indexed with its SOP title and section heading in front of the body (a contextual
chunk header), so a section like "4. Turnaround" still knows it is about special stains.

Every result carries a citation ("SOP-420 §3") that the agent's grounding guard checks: an answer
may only cite sections that a search in the same turn actually returned.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SOP_DIR = Path(__file__).resolve().parent / "sops"
Mode = Literal["bm25", "embed", "hybrid"]


@dataclass(frozen=True)
class Chunk:
    sop: str           # "SOP-420"
    title: str         # "IHC instrument QC failure and QC hold"
    version: str       # "Version 2 | Effective 2026-04-01 | Owner: ..."
    section: int       # 3
    heading: str       # "Steps after a failed QC"
    text: str          # the section body

    @property
    def citation(self) -> str:
        return f"{self.sop} §{self.section}"

    @property
    def spoken(self) -> str:
        """How to say the citation aloud: "SOP 420, section 3" (TTS reads "§" badly)."""
        return f"SOP {self.sop.split('-')[1]}, section {self.section}"

    @property
    def indexed_text(self) -> str:
        return f"{self.sop} {self.title}. {self.heading}. {self.text}"

    def public(self) -> dict[str, Any]:
        return {"citation": self.citation, "cite_as": self.spoken, "sop_title": self.title,
                "section_title": self.heading, "text": self.text}


# ------------------------------------------------------------------ corpus ---
_TITLE = re.compile(r"^#\s+(SOP-\d{3})\s+(.+)$")
_SECTION = re.compile(r"^##\s+(\d+)\.\s+(.+)$")


def load_corpus(directory: Path = SOP_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("sop-*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        m = _TITLE.match(lines[0]) if lines else None
        if not m:
            raise ValueError(f"{path.name}: first line must be '# SOP-NNN Title'")
        sop, title = m.groups()
        version = lines[1].strip() if len(lines) > 1 else ""
        current: tuple[int, str] | None = None
        body: list[str] = []

        def flush() -> None:
            if current and " ".join(body).strip():
                chunks.append(Chunk(sop, title, version, current[0], current[1], " ".join(" ".join(body).split())))

        for line in lines[2:]:
            s = _SECTION.match(line)
            if s:
                flush()
                current, body = (int(s.group(1)), s.group(2).strip()), []
            elif current:
                body.append(line.strip())
        flush()
    return chunks


# -------------------------------------------------------------------- BM25 ---
_STOP = set("""a an and are as at be by can do does for from has have how i if in is it its me my
no not of on or our should so that the their them then there these they this to us was we what
when where which who will with you your am into than too very just up out about""".split())


def _stem(w: str) -> str:
    """Deliberately tiny: fold plurals and -ing/-ed so 'controls'~'control', 'stained'~'stain'."""
    if len(w) >= 6 and w.endswith("ing"):
        return w[:-3]
    if len(w) >= 5 and w.endswith("ed"):
        return w[:-2]
    if len(w) >= 5 and w.endswith("es") and w[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return w[:-2]  # 'boxes' -> 'box', but 'slides' -> 'slide' (next rule)
    if len(w) >= 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokenize(text: str) -> list[str]:
    """'Ki-67 on IHC-01' -> ['ki67', 'ihc01']; 'H&E' -> ['he']. Hyphens and '&' join, as people
    write these terms both ways."""
    words = re.findall(r"[a-z0-9]+(?:[-&/][a-z0-9]+)*", text.lower())
    out = []
    for w in words:
        w = re.sub(r"[-&/]", "", w)
        if w and w not in _STOP:
            out.append(_stem(w))
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.tf = [Counter(d) for d in docs]
        self.avgdl = sum(len(d) for d in docs) / max(len(docs), 1)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for doc, tf in zip(self.docs, self.tf):
            norm = self.k1 * (1 - self.b + self.b * len(doc) / self.avgdl)
            out.append(sum(self.idf.get(t, 0.0) * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in query if t in tf))
        return out


# -------------------------------------------------------------- embeddings ---
class Embedder:
    """Calls an OpenAI-compatible /embeddings endpoint. Vectors for the corpus are cached on disk,
    keyed by model and text hash, so they're computed once per SOP edit, not per start."""

    # nomic-embed-text is trained with task prefixes; other models ignore them harmlessly.
    DOC_PREFIX, QUERY_PREFIX = "search_document: ", "search_query: "

    def __init__(self, model: str, base_url: str, api_key: str = "ollama", cache_path: Path | None = None,
                 timeout_s: float = 20.0):
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key
        self.cache_path, self.timeout_s = cache_path, timeout_s
        self.cache: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except (OSError, ValueError):
                self.cache = {}

    def _post(self, texts: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model, "input": texts}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read())["data"]
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]

    def documents(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha256(f"{self.model}\n{t}".encode()).hexdigest()[:24] for t in texts]
        todo = [(k, t) for k, t in zip(keys, texts) if k not in self.cache]
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            for (k, _), v in zip(batch, self._post([self.DOC_PREFIX + t for _, t in batch])):
                self.cache[k] = v
        if todo and self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache))
        return [self.cache[k] for k in keys]

    def query(self, text: str) -> list[float]:
        return self._post([self.QUERY_PREFIX + text])[0]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ------------------------------------------------------------------ search ---
def rrf(rankings: list[list[int]], k: int = 60, depth: int = 20) -> list[int]:
    """Reciprocal rank fusion: score(d) = sum over rankings of 1 / (k + rank), over each ranking's
    top `depth`. Order-only, so it needs no score calibration between BM25 and cosine similarity."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking[:depth]):
            score[doc] = score.get(doc, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda d: -score[d])


# "Purpose and scope" sections repeat the SOP's instruments and stains without saying what to do,
# so they outranked the actual procedure (e.g. SOP-420 §1 above §3 for "what are the steps").
# They stay readable through read(); search covers only sections with instructions.
_NOT_SEARCHED = {"purpose and scope"}


class SopIndex:
    def __init__(self, chunks: list[Chunk], embedder: Embedder | None = None):
        self.all_chunks = chunks
        self.chunks = [c for c in chunks if c.heading.lower() not in _NOT_SEARCHED]
        self.by_citation = {c.citation: c for c in chunks}
        self.bm25 = BM25([tokenize(c.indexed_text) for c in self.chunks])
        self.embedder = embedder
        self._vectors: list[list[float]] | None = None
        self._embed_error: str | None = None
        self._retry_at = 0.0

    @classmethod
    def from_env(cls) -> "SopIndex":
        model = os.environ.get("SOP_EMBED_MODEL", "nomic-embed-text")
        embedder = None
        if model:
            embedder = Embedder(
                model=model,
                base_url=os.environ.get("SOP_EMBED_URL", os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")),
                api_key=os.environ.get("LLM_API_KEY", "ollama"),
                cache_path=Path(os.environ.get("SOP_EMBED_CACHE", "data/sop_embeddings.json")),
            )
        return cls(load_corpus(Path(os.environ.get("SOP_DIR", SOP_DIR))), embedder)

    def _embedding_ranking(self, query: str) -> tuple[list[int], list[float]] | None:
        """Chunks ranked by cosine similarity (and the similarities), or None if embeddings are
        unavailable right now."""
        if not self.embedder or time.monotonic() < self._retry_at:
            return None
        try:
            if self._vectors is None:
                self._vectors = self.embedder.documents([c.indexed_text for c in self.chunks])
            q = self.embedder.query(query)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            self._embed_error = f"{type(exc).__name__}: {exc}"
            self._retry_at = time.monotonic() + 60  # don't stall every search on a dead endpoint
            return None
        self._embed_error = None
        sims = [_cosine(q, v) for v in self._vectors]
        return sorted(range(len(self.chunks)), key=lambda i: -sims[i]), sims

    def search(self, query: str, k: int = 3, mode: Mode = "hybrid") -> dict[str, Any]:
        q_tokens = tokenize(query)
        bm25 = self.bm25.scores(q_tokens)
        lexical = [i for i in sorted(range(len(self.chunks)), key=lambda i: -bm25[i]) if bm25[i] > 0]
        embedded = self._embedding_ranking(query) if mode in ("embed", "hybrid") else None
        semantic, sims = embedded if embedded else (None, None)

        if mode == "embed" and semantic is not None:
            order, used = semantic, "embed"
        elif mode == "hybrid" and semantic is not None:
            order, used = rrf([lexical, semantic]), "hybrid"
        else:
            order, used = lexical, "bm25"

        results = []
        for i in order[:k]:
            c = self.chunks[i]
            r = {**c.public(), "term_overlap": round(_coverage(q_tokens, c), 2)}
            if sims is not None:
                r["similarity"] = round(sims[i], 3)
            results.append(r)
        out: dict[str, Any] = {"query": query, "mode": used, "results": results}
        if mode != "bm25" and used == "bm25" and self._embed_error:
            out["note_for_operator"] = f"embeddings unavailable, keyword search only ({self._embed_error})"
        if is_weak(results):
            out["coverage"] = "weak"
            out["instruction"] = ("No SOP section clearly matches this question. Tell the user the SOPs don't "
                                  "cover it and suggest asking the supervisor. Do not answer from general knowledge.")
        else:
            out["coverage"] = "ok"
        return out

    def read(self, ref: str) -> dict[str, Any]:
        """A section by citation ('SOP-420 §3', 'SOP 420 section 3') or a whole SOP's outline ('SOP-420')."""
        m = re.search(r"(\d{3})(?:\D+(\d+))?", ref)
        if not m:
            raise KeyError(ref)
        sop = f"SOP-{m.group(1)}"
        if m.group(2):
            chunk = self.by_citation.get(f"{sop} §{int(m.group(2))}")
            if not chunk:
                raise KeyError(ref)
            return chunk.public()
        sections = [c for c in self.all_chunks if c.sop == sop]
        if not sections:
            raise KeyError(ref)
        return {"sop": sop, "title": sections[0].title, "version": sections[0].version,
                "sections": [{"citation": c.citation, "section_title": c.heading} for c in sections]}


# "Not covered" when the best chunk shares less than WEAK_MATCH of the question's words AND (when
# embeddings are on) its cosine similarity is under WEAK_SIMILARITY. Both are tuned on
# evals/sop_retrieval.jsonl, which includes questions the SOPs deliberately don't answer.
WEAK_MATCH = float(os.environ.get("SOP_WEAK_OVERLAP", "0.34"))
WEAK_SIMILARITY = float(os.environ.get("SOP_WEAK_SIMILARITY", "0.6"))


def is_weak(results: list[dict[str, Any]]) -> bool:
    if not results:
        return True
    if max(r["term_overlap"] for r in results) >= WEAK_MATCH:
        return False
    sims = [r["similarity"] for r in results if "similarity" in r]
    return not sims or max(sims) < WEAK_SIMILARITY


def _coverage(q_tokens: list[str], chunk: Chunk) -> float:
    """Share of the question's distinct content words that appear in the chunk (with its headers)."""
    q = set(q_tokens)
    if not q:
        return 0.0
    return len(q & set(tokenize(chunk.indexed_text))) / len(q)
