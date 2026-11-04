"""Retrieval eval for the SOP server: does search put the right section near the top, and does it
admit when the SOPs don't cover a question?

    python -m evals.run_retrieval                 # every mode that's available
    python -m evals.run_retrieval --mode bm25     # keyword only (no Ollama needed)

Metrics, over evals/sop_retrieval.jsonl:
- hit@1 / hit@3: an acceptable section is the first result / in the top 3 (answerable questions)
- MRR: mean of 1/rank of the first acceptable section (0 if not in the top 10)
- abstain accuracy: questions the SOPs don't cover are marked "weak", answerable ones aren't
Embedding modes need an OpenAI-compatible /embeddings endpoint (default: Ollama nomic-embed-text).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from servers.sop_index import SopIndex

CASES = Path(__file__).with_name("sop_retrieval.jsonl")


def evaluate(index: SopIndex, cases: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    rows = []
    for case in cases:
        out = index.search(case["query"], k=10, mode=mode)
        cited = [r["citation"] for r in out["results"]]
        rank = next((i + 1 for i, c in enumerate(cited) if c in case["expect"]), None)
        rows.append({"id": case["id"], "tags": case["tags"], "answerable": bool(case["expect"]),
                     "rank": rank, "weak": out["coverage"] == "weak", "top": cited[:3], "mode_used": out["mode"]})
    pos = [r for r in rows if r["answerable"]]
    neg = [r for r in rows if not r["answerable"]]

    def pct(xs: list[bool]) -> float:
        return round(100 * sum(xs) / len(xs), 1) if xs else 0.0

    by_tag = {}
    for tag in ("keyword", "paraphrase"):
        sub = [r for r in pos if tag in r["tags"]]
        by_tag[tag] = {"n": len(sub), "hit@3_%": pct([r["rank"] is not None and r["rank"] <= 3 for r in sub])}
    return {
        "mode_requested": mode,
        "mode_used": sorted({r["mode_used"] for r in rows}),
        "answerable": len(pos), "not_covered": len(neg),
        "hit@1_%": pct([r["rank"] == 1 for r in pos]),
        "hit@3_%": pct([r["rank"] is not None and r["rank"] <= 3 for r in pos]),
        "mrr": round(sum(1 / r["rank"] for r in pos if r["rank"]) / len(pos), 3),
        "abstain_acc_%": pct([r["weak"] for r in neg] + [not r["weak"] for r in pos]),
        "false_weak_on_answerable": [r["id"] for r in pos if r["weak"]],
        "missed_not_covered": [r["id"] for r in neg if not r["weak"]],
        "by_tag": by_tag,
        "misses": [{"id": r["id"], "rank": r["rank"], "top": r["top"]} for r in pos if not r["rank"] or r["rank"] > 3],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bm25", "embed", "hybrid", "all"], default="all")
    ap.add_argument("--out", default="evals/results/retrieval.json")
    a = ap.parse_args()
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    index = SopIndex.from_env()
    modes = ["bm25", "embed", "hybrid"] if a.mode == "all" else [a.mode]
    reports = [evaluate(index, cases, m) for m in modes]

    print(f"{'mode':<8} {'used':<8} {'hit@1':>6} {'hit@3':>6} {'MRR':>6} {'abstain':>8}  keyword/paraphrase hit@3")
    for r in reports:
        t = r["by_tag"]
        print(f"{r['mode_requested']:<8} {','.join(r['mode_used']):<8} {r['hit@1_%']:>6} {r['hit@3_%']:>6} "
              f"{r['mrr']:>6} {r['abstain_acc_%']:>8}  {t['keyword']['hit@3_%']}/{t['paraphrase']['hit@3_%']}")
    for r in reports:
        if r["misses"] or r["missed_not_covered"] or r["false_weak_on_answerable"]:
            print(f"\n[{r['mode_requested']}] misses: {[m['id'] for m in r['misses']]}")
            print(f"[{r['mode_requested']}] not-covered questions answered anyway: {r['missed_not_covered']}")
            print(f"[{r['mode_requested']}] answerable questions marked weak: {r['false_weak_on_answerable']}")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
