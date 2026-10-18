"""Grounding guard: answers that state lab facts must be backed by this turn's tool results."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import anyio
import pytest

from voiceops.agent.loop import UNGROUNDED_FALLBACK, Agent, grounding_problem
from voiceops.agent.store import Store
from voiceops.config import default_servers
from voiceops.llm import AssistantMessage, ScriptedLLM, ToolCallRequest
from voiceops.mcp_host import MCPHost

# The exact confabulation observed in the eval run (no tool was called).
CONFABULATED = "S26-00312 is overdue and blocked by a labeling discrepancy."


def call(name: str, args: dict, id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=id, name=name, arguments=json.dumps(args))


def run(body):
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                await body(host, Store(str(Path(d) / "agent.db")))
    anyio.run(main)


@pytest.mark.parametrize("answer,evidence,bad", [
    (CONFABULATED, [], True),                                              # facts, no tool at all
    ("IHC-02 failed QC.", ['{"id": "IHC-01", "status": "qc_hold"}'], True),  # names an ID no tool returned
    ("S26-00312 is waiting on HER2; IHC-01 is on QC hold.", ['{"accession": "S26-00312", "x": "IHC-01"}'], False),
    ("You're welcome!", [], False),
    ("That's the pathologist's call, not mine.", [], False),
    ("Sorry, I can only help with lab work.", [], False),
    ("Do you mean IHC stainer 1 or 2?", [], False),
])
def test_grounding_problem(answer, evidence, bad):
    assert (grounding_problem(answer, evidence) is not None) == bad


def test_confabulated_answer_is_retried_with_a_tool_call():
    async def body(host, store):
        llm = ScriptedLLM([
            AssistantMessage(content=CONFABULATED),                                   # skips the lookup
            AssistantMessage(tool_calls=[call("lims__get_specimen", {"accession": "312"})]),
            AssistantMessage(content="S26-00312 is waiting on HER2, and IHC-01 is on QC hold."),
        ])
        res = await Agent(llm, host, store).run_turn("Why isn't 312 ready yet?")
        assert res.answer.startswith("S26-00312 is waiting on HER2")
        kinds = [e["kind"] for e in res.trace]
        assert "grounding_retry" in kinds and "grounding_blocked" not in kinds
        assert "[grounding check]" in llm.requests[1][-1]["content"], "the model is told what was wrong"
        saved = json.dumps(store.load_messages(res.session_id))
        assert CONFABULATED not in saved and "[grounding check]" not in saved, "neither is kept in history"
    run(body)


def test_repeated_confabulation_is_replaced_not_spoken():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(content=CONFABULATED), AssistantMessage(content=CONFABULATED)])
        res = await Agent(llm, host, store).run_turn("Why isn't 312 ready yet?")
        assert res.answer == UNGROUNDED_FALLBACK
        assert "grounding_blocked" in [e["kind"] for e in res.trace]
    run(body)


def test_answer_naming_an_unseen_id_is_retried():
    async def body(host, store):
        llm = ScriptedLLM([
            AssistantMessage(tool_calls=[call("lims__get_instrument", {"instrument": "IHC stainer one"})]),
            AssistantMessage(content="IHC-02 failed QC this morning."),               # wrong instrument
            AssistantMessage(content="IHC-01 failed QC this morning."),
        ])
        res = await Agent(llm, host, store).run_turn("Did IHC stainer one pass QC?")
        assert res.answer == "IHC-01 failed QC this morning."
    run(body)


def test_grounded_and_non_lab_answers_pass_untouched():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(content="You're welcome!")])
        res = await Agent(llm, host, store).run_turn("thanks")
        assert res.answer == "You're welcome!" and len(llm.requests) == 1
    run(body)
