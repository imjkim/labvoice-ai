"""The SOP server: corpus parsing, keyword + embedding search fused with RRF, graceful fallback
when embeddings are down, "not covered" detection, and the MCP tools as the agent sees them."""
from __future__ import annotations

import json
import re
import tempfile
import urllib.error
from pathlib import Path

import anyio
import pytest

from servers.sop_index import BM25, Embedder, SopIndex, load_corpus, rrf, tokenize
from voiceops.config import default_servers
from voiceops.mcp_host import MCPHost

CHUNKS = load_corpus()


def test_corpus_parses_into_cited_sections():
    sops = {c.sop for c in CHUNKS}
    assert len(sops) == 11 and len(CHUNKS) >= 45
    assert len({c.citation for c in CHUNKS}) == len(CHUNKS), "citations are unique"
    for c in CHUNKS:
        assert re.fullmatch(r"SOP-\d{3} §\d+", c.citation)
        assert c.version.startswith("Version") and len(c.text.split()) >= 12, c.citation
    assert next(c for c in CHUNKS if c.citation == "SOP-420 §3").spoken == "SOP 420, section 3"


@pytest.mark.parametrize("text,tokens", [
    ("Ki-67 on IHC-01", ["ki67", "ihc01"]),
    ("H&E stainer", ["he", "stainer"]),
    ("What controls do the slides need?", ["control", "slide", "need"]),
])
def test_tokenize_joins_lab_terms_and_folds_plurals(text, tokens):
    assert tokenize(text) == tokens


def test_bm25_prefers_documents_with_rare_query_terms():
    bm = BM25([["stain", "control"], ["coverslipper", "jam"], ["stain", "slide"]])
    scores = bm.scores(["coverslipper"])
    assert scores.index(max(scores)) == 1 and scores[0] == scores[2] == 0


def test_rrf_rewards_agreement_between_rankings():
    assert rrf([[0, 1, 2], [2, 0, 1]]) == [0, 2, 1]
    assert rrf([[5], []]) == [5], "a document only one ranking found still counts"


class ConceptEmbedder(Embedder):
    """Stand-in for a real embedding model: maps synonyms to shared dimensions, so paraphrases land
    close together. Lets the fusion path be tested without Ollama."""
    CONCEPTS = {"fail": "fail", "flunk": "fail", "check": "qc", "qc": "qc", "ihc": "ihc", "ihc01": "ihc",
                "stainer": "ihc", "step": "steps", "now": "steps", "jam": "jam"}

    def __init__(self):
        super().__init__(model="concepts", base_url="http://unused")
        self.calls = 0

    def _vec(self, text):
        dims = sorted(set(self.CONCEPTS.values()))
        words = {self.CONCEPTS.get(t) for t in tokenize(text)}
        return [1.0 if d in words else 0.0 for d in dims]

    def _post(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]


def test_embeddings_find_a_paraphrase_keyword_search_misses():
    q = "IHC-01 flunked its morning check, now what"
    keyword = SopIndex(CHUNKS).search(q, k=3, mode="bm25")
    assert "SOP-420 §3" not in [r["citation"] for r in keyword["results"]]
    semantic = SopIndex(CHUNKS, ConceptEmbedder()).search(q, k=3, mode="embed")
    assert semantic["mode"] == "embed" and semantic["results"][0]["citation"] == "SOP-420 §3"
    hybrid = SopIndex(CHUNKS, ConceptEmbedder()).search(q, k=3, mode="hybrid")
    assert hybrid["mode"] == "hybrid" and all("similarity" in r for r in hybrid["results"])
    # Whether fusion beats either list alone is an empirical question for evals/run_retrieval.py.


def test_dead_embedding_endpoint_falls_back_to_keywords_and_backs_off():
    class Down(Embedder):
        calls = 0

        def _post(self, texts):
            Down.calls += 1
            raise urllib.error.URLError("connection refused")

    index = SopIndex(CHUNKS, Down(model="m", base_url="http://127.0.0.1:9"))
    first = index.search("the coverslipper jammed", mode="hybrid")
    assert first["mode"] == "bm25" and "embeddings unavailable" in first["note_for_operator"]
    assert first["results"][0]["citation"] == "SOP-610 §4"
    index.search("water bath temperature", mode="hybrid")
    assert Down.calls == 1, "doesn't stall every search retrying a dead endpoint"


def test_document_vectors_are_cached_on_disk():
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d) / "vectors.json"
        first = ConceptEmbedder(); first.cache_path = cache
        SopIndex(CHUNKS, first).search("coverslipper jam", mode="embed")
        second = ConceptEmbedder(); second.cache_path = cache; second.cache = json.loads(cache.read_text())
        SopIndex(CHUNKS, second).search("coverslipper jam", mode="embed")
        assert first.calls >= 2 and second.calls == 1, "the second start only embeds the query"


@pytest.mark.parametrize("question,covered", [
    ("the coverslipper jammed", True),
    ("how thick do I cut kidney biopsies", True),
    ("can you order lunch for the team", False),
    ("how many vacation days do I get", False),
])
def test_questions_the_sops_dont_cover_are_marked_weak(question, covered):
    out = SopIndex(CHUNKS).search(question, mode="bm25")
    assert (out["coverage"] == "ok") == covered
    if not covered:
        assert "don't cover" in out["instruction"]


def test_read_by_citation_or_outline():
    index = SopIndex(CHUNKS)
    assert index.read("SOP 420 section 3")["citation"] == "SOP-420 §3"
    assert index.read("SOP-420 §3")["section_title"] == "Steps after a failed QC"
    outline = index.read("SOP-610")
    assert outline["title"].startswith("Instrument downtime") and len(outline["sections"]) == 4
    for bad in ("SOP-999", "SOP-420 §9", "the IHC one"):
        with pytest.raises(KeyError):
            index.read(bad)


def run(body):
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"), sop_embeddings=False)) as host:
                await body(host)
    anyio.run(main)


def test_sop_tools_are_read_only_and_cite_sections_through_the_host():
    async def body(host):
        assert host.tools["sops__search_sops"].read_only and host.tools["sops__read_sop"].read_only
        found = await host.call("sops__search_sops", {"question": "IHC stainer failed QC, what are the steps"})
        assert found.ok and found.data["coverage"] == "ok"
        by_citation = {r["citation"]: r for r in found.data["results"]}
        assert "SOP-420 §3" in by_citation, "the steps section is among what the model reads"
        assert by_citation["SOP-420 §3"]["cite_as"] == "SOP 420, section 3"
        missing = await host.call("sops__read_sop", {"ref": "SOP-999 §2"})
        assert not missing.ok and "search_sops" in missing.error, "the error tells the model what to do"
    run(body)


def test_no_real_sop_citations_in_model_visible_text():
    """Same rule as for specimens (DECISIONS B3): an example the model can reuse must not be real."""
    from voiceops.agent.loop import SYSTEM_PROMPT

    real = {c.sop.split("-")[1] for c in CHUNKS}

    async def body(host):
        texts = {"system prompt": SYSTEM_PROMPT}
        for name, tool in host.tools.items():
            texts[name] = tool.description + " " + json.dumps(tool.model_schema(), ensure_ascii=False)
        leaks = [f"{src}: SOP {n}" for src, t in texts.items() for n in re.findall(r"SOP[\s-]?(\d{3})", t) if n in real]
        assert not leaks, leaks
    run(body)
