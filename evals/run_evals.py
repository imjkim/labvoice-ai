"""Eval harness: runs every case through the real agent + MCP stack and scores it.

    python -m evals.run_evals                       # model from .env
    python -m evals.run_evals --model qwen3:8b --runs 3
    python -m evals.run_evals --fault-rate 0.3      # resilience under injected tool failures

Each case runs in a fresh session against a fresh copy of the LIMS database.
Results are written to evals/results/<model>-<timestamp>.json so you can
compare models and prompt changes over time (put the table in your README).
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio

from voiceops.agent.loop import Agent, TurnResult
from voiceops.agent.store import Store
from voiceops.config import default_servers, settings
from voiceops.mcp_host import MCPHost

HERE = Path(__file__).parent


def score_case(case: dict[str, Any], res: TurnResult, write_tools: set[str]) -> dict[str, Any]:
    called = [c["name"] for c in res.tool_calls]
    allowed_extra = set(case.get("allow_tools", []))
    checks: dict[str, bool] = {}

    # Tool selection: every expected slot ("a|b" = either) was called; nothing forbidden;
    # and if no tools were expected, none were called (other than explicitly allowed ones).
    expected = case.get("expect_tools", [])
    slots_ok = all(any(alt in called for alt in slot.split("|")) for slot in expected)
    forbidden_ok = not (set(case.get("forbid_tools", [])) & set(called))
    none_ok = bool(expected) or not (set(called) - allowed_extra)
    checks["tool_selection"] = slots_ok and forbidden_ok and none_ok

    if case.get("expect_args"):
        checks["arguments"] = all(_args_match(res, tool, want) for tool, want in case["expect_args"].items())

    pending = bool(res.pending_actions)
    if "expect_pending" in case:
        checks["approval_gate"] = pending == case["expect_pending"]
    if case.get("expect_pending_if_called"):
        checks["approval_gate"] = pending == bool(set(called) & write_tools)

    answer = res.answer.lower()
    if case.get("answer_must_include"):
        checks["answer"] = all(s.lower() in answer for s in case["answer_must_include"])
    if case.get("answer_must_not_include"):
        checks["answer"] = checks.get("answer", True) and not any(
            s.lower() in answer for s in case["answer_must_not_include"])

    # Safety invariant: a write tool must never report "ok" inside a turn (only via approval).
    unsafe = sum(1 for c in res.tool_calls if c["name"] in write_tools and c["status"] == "ok")

    return {
        "id": case["id"], "tags": case.get("tags", []), "pass": all(checks.values()) and unsafe == 0, "checks": checks,
        "unsafe_writes": unsafe, "called": called, "answer": res.answer,
        "latency_ms": res.latency_ms,
        "llm_ms": sum(e["latency_ms"] or 0 for e in res.trace if e["kind"] == "llm"),
        "llm_calls": sum(1 for e in res.trace if e["kind"] == "llm"),
        "tool_errors": sum(1 for c in res.tool_calls if c["status"] not in ("ok", "pending_approval")),
    }


def _args_match(res: TurnResult, tool: str, want: dict[str, Any]) -> bool:
    for c in res.tool_calls:
        if c["name"] == tool and isinstance(c["arguments"], dict):
            got = c["arguments"]
            if all(k in got and str(v).lower() in str(got[k]).lower() for k, v in want.items()):
                return True
    return False


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(key: str) -> float | None:
        vals = [r["checks"][key] for r in rows if key in r["checks"]]
        return round(100 * sum(vals) / len(vals), 1) if vals else None

    lat = sorted(r["latency_ms"] for r in rows)
    return {
        "cases": len(rows),
        "pass_rate_%": round(100 * sum(r["pass"] for r in rows) / len(rows), 1),
        "tool_selection_%": rate("tool_selection"),
        "argument_accuracy_%": rate("arguments"),
        "approval_gate_%": rate("approval_gate"),
        "answer_checks_%": rate("answer"),
        "unsafe_writes": sum(r["unsafe_writes"] for r in rows),
        "turn_p50_ms": round(statistics.median(lat), 1),
        "turn_p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1),
        "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in rows), 2),
        "pass_by_tag_%": {t: round(100 * sum(r["pass"] for r in rows if t in r["tags"]) /
                                   sum(1 for r in rows if t in r["tags"]), 1)
                          for t in sorted({t for r in rows for t in r["tags"]})},
    }


async def run(cases: list[dict[str, Any]], model: str | None, runs: int, fault_rate: str) -> dict[str, Any]:
    from voiceops.llm import OpenAICompatLLM

    llm = OpenAICompatLLM(model=model)
    rows: list[dict[str, Any]] = []
    for r in range(runs):
        for case in cases:
            with tempfile.TemporaryDirectory() as d:
                async with MCPHost(default_servers(str(Path(d) / "lims.db"), fault_rate)) as host:
                    writes = {n for n, t in host.tools.items() if not t.read_only}
                    agent = Agent(llm, host, Store(str(Path(d) / "agent.db")))
                    res = await agent.run_turn(case["input"])
                    row = score_case(case, res, writes) | {"run": r}
                    rows.append(row)
                    print(f"{'PASS' if row['pass'] else 'FAIL'}  {case['id']:<28} {row['latency_ms']:>8.0f} ms  "
                          f"called={row['called']}  {'' if row['pass'] else row['checks']}")
    return {"model": llm.model, "runs": runs, "fault_rate": fault_rate,
            "summary": summarize(rows), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--fault-rate", default="0")
    ap.add_argument("--only", default=None, help="comma-separated case ids")
    ap.add_argument("--tag", default=None, help="only cases with this tag")
    a = ap.parse_args()

    cases = [json.loads(l) for l in (HERE / "cases.jsonl").read_text().splitlines() if l.strip()]
    if a.only:
        cases = [c for c in cases if c["id"] in a.only.split(",")]
    if a.tag:
        cases = [c for c in cases if a.tag in c.get("tags", [])]
    report = anyio.run(run, cases, a.model, a.runs, a.fault_rate)

    out = HERE / "results" / f"{report['model'].replace(':', '_').replace('/', '_')}-{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print("\n" + "\n".join(f"{k:<22} {v}" for k, v in report["summary"].items()))
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
