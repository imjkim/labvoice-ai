"""Behavioral tests for the agent loop, approval boundary and MCP host.

They use a ScriptedLLM (deterministic) against the *real* LIMS MCP server
subprocess and real SQLite, so they test the plumbing that matters without
a GPU or API key. Model quality is measured separately in evals/.

Run: pytest -q
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import anyio

from voiceops.agent.loop import Agent
from voiceops.agent.store import ActionError, Store
from voiceops.config import default_servers
from voiceops.llm import AssistantMessage, ScriptedLLM, ToolCallRequest
from voiceops.mcp_host import HostedTool, MCPHost

STAR = "S26-00312"  # planted: STAT breast biopsy, HER2 stuck on IHC-01 (failed QC)


def call(name: str, args: dict, id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(id=id, name=name, arguments=json.dumps(args))


def run(test_body, *, fault_rate: str = "0"):
    """Fresh LIMS db + agent db per test, then run an async body."""
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"), fault_rate)) as host:
                await test_body(host, Store(str(Path(d) / "agent.db")))
    anyio.run(main)


async def orders_for(host: MCPHost, accession: str) -> list[dict]:
    return (await host.call("lims__get_specimen", {"accession": accession})).data["stain_orders"]


def test_tools_discovered_with_policy():
    async def body(host, store):
        reads = {"lims__get_specimen", "lims__find_specimens", "lims__get_instrument", "lims__list_instruments"}
        writes = {"lims__order_stain", "lims__flag_specimen", "lims__set_instrument_status"}
        assert reads | writes <= set(host.tools)
        assert all(host.tools[t].read_only for t in reads)
        assert not any(host.tools[t].read_only for t in writes)
    run(body)


def test_multi_hop_read_path():
    """'Why is 312 late?' -> specimen shows HER2 on IHC-01 -> instrument shows failed QC."""
    async def body(host, store):
        llm = ScriptedLLM([
            AssistantMessage(tool_calls=[call("lims__get_specimen", {"accession": "S 26 312"})]),
            AssistantMessage(tool_calls=[call("lims__get_instrument", {"instrument": "IHC-01"}, id="c2")]),
            AssistantMessage(content="HER2 is stuck on IHC-01, which failed QC this morning."),
        ])
        res = await Agent(llm, host, store).run_turn("why is 312 not ready?")
        assert [c["status"] for c in res.tool_calls] == ["ok", "ok"]
        assert '"instrument_id": "IHC-01"' in llm.requests[1][-1]["content"]
        assert '"result": "fail"' in llm.requests[2][-1]["content"]
    run(body)


def test_write_is_held_then_executes_exactly_once():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(tool_calls=[
            call("lims__order_stain", {"accession": STAR, "stain": "Ki-67", "block": "A2", "reason": "per Dr. Ruiz"})])])
        agent = Agent(llm, host, store)
        n_before = len(await orders_for(host, STAR))

        res = await agent.run_turn("order a Ki-67 on block A2 of 312")
        assert res.tool_calls[0]["status"] == "pending_approval"
        assert "approval" in res.answer
        assert len(await orders_for(host, STAR)) == n_before, "write must NOT run before approval"

        action_id = res.pending_actions[0]["action_id"]
        out = await agent.resolve_action(action_id, approve=True)
        assert out["status"] == "executed" and out["result"]["stain"] == "Ki-67"
        try:
            await agent.resolve_action(action_id, approve=True)
            raise AssertionError("second approval must fail")
        except ActionError:
            pass
        assert len(await orders_for(host, STAR)) == n_before + 1
        assert "[system note]" in store.load_messages(res.session_id)[-1]["content"]
    run(body)


def test_preflight_rejects_invalid_write_before_a_human_sees_it():
    async def body(host, store):
        llm = ScriptedLLM([
            AssistantMessage(tool_calls=[call("lims__order_stain", {"accession": "312", "stain": "PAS", "block": "A7"})]),
            AssistantMessage(content="312 only has blocks A1 and A2. Which one?"),
        ])
        res = await Agent(llm, host, store).run_turn("order PAS on A7 for 312")
        assert res.tool_calls[0]["status"] == "rejected_preflight"
        assert not res.pending_actions
        assert "has blocks A1, A2" in llm.requests[1][-1]["content"]
    run(body)


def test_preview_warns_approver_about_duplicate_order():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(tool_calls=[
            call("lims__order_stain", {"accession": STAR, "stain": "her2", "block": "A1"})])])
        res = await Agent(llm, host, store).run_turn("order a HER2 on 312")
        preview = res.pending_actions[0]["preview"]
        assert preview["stain"] == "HER2" and "already in progress" in preview["warning"]
        assert "already in progress" in res.answer
    run(body)


def test_model_never_sees_dry_run_and_cannot_set_it():
    async def body(host, store):
        schema = host.tools["lims__order_stain"].openai_schema()["function"]["parameters"]
        assert "dry_run" not in schema["properties"]
        llm = ScriptedLLM([AssistantMessage(tool_calls=[
            call("lims__flag_specimen", {"accession": STAR, "reason": "label smudged", "dry_run": True})])])
        agent = Agent(llm, host, store)
        res = await agent.run_turn("flag 312")
        assert "dry_run" not in res.pending_actions[0]["args"]
        out = await agent.resolve_action(res.pending_actions[0]["action_id"], approve=True)
        assert out["status"] == "executed" and "dry_run" not in out["result"]
        assert (await host.call("lims__get_specimen", {"accession": STAR})).data["flag"] == "label smudged"
    run(body)


def test_denied_action_never_executes():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(tool_calls=[
            call("lims__flag_specimen", {"accession": "S26-00287", "reason": "cassette count mismatch"})])])
        agent = Agent(llm, host, store)
        res = await agent.run_turn("flag 287")
        out = await agent.resolve_action(res.pending_actions[0]["action_id"], approve=False)
        assert out["status"] == "denied"
        spec = (await host.call("lims__get_specimen", {"accession": "S26-00287"})).data
        assert spec["flag"] is None
    run(body)


def test_execution_revalidates_if_state_changed_after_approval_request():
    """Preflight passed, then the world changed (a QC run failed) before the supervisor approved.
    Execution re-validates against the system of record and fails safely."""
    async def main():
        with tempfile.TemporaryDirectory() as d:
            lims_db = str(Path(d) / "lims.db")
            async with MCPHost(default_servers(lims_db)) as host:
                llm = ScriptedLLM([AssistantMessage(tool_calls=[call(
                    "lims__set_instrument_status", {"instrument": "HE-01", "status": "online", "note": "pump replaced"})])])
                agent = Agent(llm, host, Store(str(Path(d) / "agent.db")))
                res = await agent.run_turn("bring the H&E stainer back online")
                assert res.pending_actions[0]["preview"]["to"] == "online"

                with closing(sqlite3.connect(lims_db)) as c, c:  # meanwhile, a tech logs a failed QC run
                    c.execute("INSERT INTO qc_runs (instrument_id, run_at, result, notes, performed_by) "
                              "VALUES ('HE-01', '2999-01-01T00:00+00:00', 'fail', 'eosin too pale', 'R. Patel')")

                out = await agent.resolve_action(res.pending_actions[0]["action_id"], approve=True)
                assert out["status"] == "failed" and "QC failed" in out["result"]["error"]
    anyio.run(main)


def test_invalid_args_are_rejected_before_execution_or_approval():
    async def body(host, store):
        llm = ScriptedLLM([
            AssistantMessage(tool_calls=[call("lims__set_instrument_status",
                                              {"instrument": "HE-01", "status": "broken", "note": "x"})]),
            AssistantMessage(tool_calls=[ToolCallRequest(id="c2", name="lims__get_specimen", arguments="{not json")]),
            AssistantMessage(content="Sorry, I couldn't do that."),
        ])
        res = await Agent(llm, host, store).run_turn("x")
        assert [c["status"] for c in res.tool_calls] == ["invalid_args", "invalid_args"]
        assert not res.pending_actions, "garbage must never reach a human approver"
        assert "Invalid arguments" in llm.requests[1][-1]["content"]
    run(body)


def test_unknown_tool_is_reported_to_model():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(tool_calls=[call("lims__delete_specimen", {"accession": STAR})]),
                           AssistantMessage(content="I can't do that.")])
        res = await Agent(llm, host, store).run_turn("delete 312")
        assert res.tool_calls[0]["status"] == "unknown_tool"
    run(body)


def test_tool_error_reaches_model_and_is_not_retried():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(tool_calls=[call("lims__get_specimen", {"accession": "S26-99999"})]),
                           AssistantMessage(content="I couldn't find that specimen.")])
        res = await Agent(llm, host, store).run_turn("status of 99999")
        assert "No specimen S26-99999" in llm.requests[1][-1]["content"]
        assert res.tool_calls[0]["attempts"] == 1, "deterministic errors are not retried"
    run(body)


def test_transient_read_failure_retries_once_writes_never():
    async def body(host, store):
        read = await host.call("lims__get_specimen", {"accession": STAR})
        assert not read.ok and read.attempts == 2 and "injected" in read.error
        write = await host.call("lims__flag_specimen", {"accession": STAR, "reason": "test reason"})
        assert write.attempts == 1
    run(body, fault_rate="1")


def test_conversation_state_persists_across_turns():
    async def body(host, store):
        llm = ScriptedLLM([AssistantMessage(content="Hi."), AssistantMessage(content="Still here.")])
        agent = Agent(llm, host, store)
        first = await agent.run_turn("hello")
        await agent.run_turn("again", session_id=first.session_id)
        assert [m["content"] for m in llm.requests[1] if m["role"] == "user"] == ["hello", "again"]
    run(body)


def test_untrusted_server_hint_is_ignored():
    from mcp.types import ToolAnnotations
    t = HostedTool("thirdparty", "wipe", "", {}, ToolAnnotations(read_only_hint=True), trusted=False)
    assert not t.read_only, "a third-party server claiming read-only must still require approval"
    t2 = HostedTool("lims", "x", "", {}, None, trusted=True)
    assert not t2.read_only, "missing annotations fail closed"
