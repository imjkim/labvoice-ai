"""Approval roles: who may approve what, four-eyes, withdrawal, and audit-trail attribution."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import anyio
import pytest

from voiceops.agent.loop import ActionForbidden, Agent
from voiceops.agent.store import Store
from voiceops.auth import DEFAULT_APPROVERS, DEMO_USERS, check_decision
from voiceops.config import default_servers
from voiceops.llm import AssistantMessage, ScriptedLLM, ToolCallRequest
from voiceops.mcp_host import MCPHost

TECH, LEAD, SUP, PATH = (DEMO_USERS[u] for u in ("mchen", "rpatel", "dreyes", "cruiz"))
STAR = "S26-00312"


def call(name: str, args: dict) -> ToolCallRequest:
    return ToolCallRequest(id="c1", name=name, arguments=json.dumps(args))


def run(body):
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                await body(host, Store(str(Path(d) / "agent.db")))
    anyio.run(main)


async def propose(host, store, tool_call: ToolCallRequest, requester) -> tuple[Agent, dict]:
    agent = Agent(ScriptedLLM([AssistantMessage(tool_calls=[tool_call])]), host, store)
    res = await agent.run_turn("do it", user=requester)
    assert res.pending_actions, res.tool_calls
    return agent, res


ORDER = call("lims__order_stain", {"accession": STAR, "stain": "Ki-67", "block": "A2"})
HOLD = call("lims__set_instrument_status", {"instrument": "IHC-02", "status": "qc_hold", "note": "bad reagent lot"})


def test_wrong_role_is_forbidden_and_does_not_consume_the_action():
    async def body(host, store):
        agent, res = await propose(host, store, ORDER, TECH)
        action_id = res.pending_actions[0]["action_id"]
        with pytest.raises(ActionForbidden, match="pathologist, supervisor"):
            await agent.resolve_action(action_id, approve=True, by=LEAD)
        assert store.load_action(action_id).status == "pending", "a forbidden attempt must not claim the action"
        out = await agent.resolve_action(action_id, approve=True, by=PATH)
        assert out["status"] == "executed"
        kinds = [e["kind"] for e in store.get_trace(res.session_id)]
        assert "approval_forbidden" in kinds, "forbidden attempts are audited"
    run(body)


def test_nobody_approves_their_own_request():
    async def body(host, store):
        agent, res = await propose(host, store, ORDER, PATH)  # a pathologist asks by voice
        action_id = res.pending_actions[0]["action_id"]
        with pytest.raises(ActionForbidden, match="your own request"):
            await agent.resolve_action(action_id, approve=True, by=PATH)
        assert (await agent.resolve_action(action_id, approve=True, by=SUP))["status"] == "executed"
    run(body)


def test_only_supervisors_change_instrument_status():
    async def body(host, store):
        agent, res = await propose(host, store, HOLD, TECH)
        action_id = res.pending_actions[0]["action_id"]
        for who in (LEAD, PATH):
            with pytest.raises(ActionForbidden):
                await agent.resolve_action(action_id, approve=True, by=who)
        assert (await agent.resolve_action(action_id, approve=True, by=SUP))["status"] == "executed"
    run(body)


def test_requester_can_withdraw_their_own_request():
    async def body(host, store):
        agent, res = await propose(host, store, HOLD, TECH)
        out = await agent.resolve_action(res.pending_actions[0]["action_id"], approve=False, by=TECH)
        assert out["status"] == "denied"
        instrument = (await host.call("lims__get_instrument", {"instrument": "IHC-02"})).data
        assert instrument["status"] == "online"
    run(body)


def test_approver_is_recorded_in_the_lims_audit_trail():
    async def body(host, store):
        agent, res = await propose(host, store, ORDER, TECH)
        await agent.resolve_action(res.pending_actions[0]["action_id"], approve=True, by=PATH)
        spec = (await host.call("lims__get_specimen", {"accession": STAR})).data
        expected = "Dr. Carmen Ruiz (pathologist) via LabVoice"
        assert spec["recent_events"][0]["actor"] == expected
        assert spec["stain_orders"][-1]["ordered_by"] == expected
    run(body)


def test_model_cannot_set_the_actor():
    async def body(host, store):
        schema = host.tools["lims__order_stain"].openai_schema()["function"]["parameters"]
        assert "actor" not in schema["properties"]
        forged = call("lims__order_stain", {"accession": STAR, "stain": "PAS", "block": "A1",
                                            "actor": "Dr. Somebody Else"})
        agent, res = await propose(host, store, forged, TECH)
        assert "actor" not in res.pending_actions[0]["args"]
        await agent.resolve_action(res.pending_actions[0]["action_id"], approve=True, by=SUP)
        spec = (await host.call("lims__get_specimen", {"accession": STAR})).data
        assert spec["stain_orders"][-1]["ordered_by"] == "D. Reyes (supervisor) via LabVoice"
    run(body)


def test_pending_answer_names_who_can_approve():
    async def body(host, store):
        _, res = await propose(host, store, ORDER, TECH)
        assert res.pending_actions[0]["approvers"] == ["pathologist", "supervisor"]
        assert res.pending_actions[0]["requested_by"] == "mchen"
        assert "approval from a pathologist or supervisor" in res.answer
    run(body)


def test_unlisted_write_tools_default_to_most_senior_role():
    assert DEFAULT_APPROVERS == frozenset({"supervisor"})
    assert check_decision("thirdparty__wipe_disk", "mchen", PATH, approve=True) is not None
    assert check_decision("thirdparty__wipe_disk", "mchen", SUP, approve=True) is None


def test_store_migrates_old_actions_table():
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "old.db")
        with closing(sqlite3.connect(path)) as c, c:
            c.execute("CREATE TABLE actions (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool TEXT NOT NULL, "
                      "args TEXT NOT NULL, status TEXT NOT NULL, preview TEXT, created_at REAL NOT NULL, "
                      "resolved_at REAL, resolved_by TEXT, result TEXT)")
        store = Store(path)
        action = store.create_action("s", "lims__flag_specimen", {}, requested_by="mchen", approvers=["supervisor"])
        assert store.load_action(action.id).requested_by == "mchen"
