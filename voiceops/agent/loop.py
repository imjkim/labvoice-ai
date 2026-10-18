"""The agent loop: model proposes tool calls, trusted code decides what runs.

    user text ─► LLM ─► tool_calls? ─no─► answer
                  ▲          │yes
                  │          ▼
                  │   validate args (JSON Schema)
                  │          │
                  │   read-only? ──yes──► execute via MCP (timeout, retry) ─┐
                  │          │no                                             │
                  │          ▼                                               │
                  │   create pending action, DO NOT execute ─► stop turn,    │
                  │                                            ask human     │
                  └──────────────── tool results ◄──────────────────────────┘
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from dataclasses import asdict, dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from voiceops.agent.store import Store
from voiceops.config import settings
from voiceops.llm import LLM, AssistantMessage
from voiceops.mcp_host import MCPHost

_YY = f"S{datetime.now().year % 100:02d}"

SYSTEM_PROMPT = f"""You are VoiceOps, a hands-free assistant for histology lab staff working at the bench.

- Use the LIMS tools for anything about specimens, stains, instruments or QC. Never guess lab data.
- Accession numbers look like {_YY}-00312; staff often say just the last digits ("312"). Pass what they said
  to the tools as-is (they normalize it), and read the full accession back in your answer.
- Your replies are spoken aloud: 1-3 short sentences, plain text, no markdown, no lists, no IDs you weren't asked for.
- When something is delayed, find the cause (e.g. a stain waiting on an instrument that failed QC) before answering.
- Tools that change the LIMS (ordering stains, flagging specimens, changing instrument status) are held for
  supervisor approval by the system. Never say an action was done unless a tool result confirms it.
- If a tool returns an error, say briefly what failed. If a request is ambiguous, ask one short question.
- You never have patient names or identifiers, and must not ask for them.
"""


@dataclass
class TurnResult:
    session_id: str
    turn_id: str
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Agent:
    def __init__(self, llm: LLM, host: MCPHost, store: Store, system_prompt: str = SYSTEM_PROMPT):
        self.llm, self.host, self.store, self.system_prompt = llm, host, store, system_prompt

    async def run_turn(self, user_text: str, session_id: str | None = None) -> TurnResult:
        session_id = session_id or str(uuid.uuid4())
        turn_id = str(uuid.uuid4())[:8]
        t_turn = time.perf_counter()
        result = TurnResult(session_id=session_id, turn_id=turn_id, answer="")

        def trace(kind: str, **kw: Any) -> None:
            result.trace.append(self.store.trace(session_id, turn_id, kind, **kw))

        messages = self.store.load_messages(session_id) or [{"role": "system", "content": self.system_prompt}]
        messages.append({"role": "user", "content": user_text})
        trace("user", payload=user_text)
        tools = self.host.openai_tools()

        for _step in range(settings.max_agent_steps):
            t0 = time.perf_counter()
            try:
                reply: AssistantMessage = await self.llm.chat(messages, tools)
            except Exception as exc:
                trace("llm", name=self.llm.model, payload={"error": str(exc)}, latency_ms=_ms(t0), ok=False)
                result.answer = "Sorry, the language model is unavailable right now."
                break
            trace("llm", name=self.llm.model, latency_ms=_ms(t0), ok=True,
                  payload={"tool_calls": [c.name for c in reply.tool_calls], "usage": reply.usage})

            if not reply.tool_calls:
                result.answer = reply.content
                messages.append({"role": "assistant", "content": reply.content})
                break

            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in reply.tool_calls
                ],
            })
            # Every tool_call id must get a tool message back, or the next request is invalid.
            for call in reply.tool_calls:
                content = await self._handle_call(call.name, call.arguments, session_id, result, trace)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

            if result.pending_actions:
                # Deterministic, not model-written: the user hears exactly what is awaiting approval.
                result.answer = " ".join(_describe_pending(a) for a in result.pending_actions)
                messages.append({"role": "assistant", "content": result.answer})
                break
        else:
            result.answer = "I couldn't finish that within my step limit. Could you narrow the request?"
            messages.append({"role": "assistant", "content": result.answer})

        self.store.save_messages(session_id, messages)
        result.latency_ms = _ms(t_turn)
        trace("answer", payload=result.answer, latency_ms=result.latency_ms, ok=True)
        return result

    async def _handle_call(self, name: str, raw_args: str, session_id: str, result: TurnResult, trace) -> str:
        record: dict[str, Any] = {"name": name, "arguments": raw_args, "status": None}
        result.tool_calls.append(record)

        tool = self.host.tools.get(name)
        if tool is None:
            record["status"] = "unknown_tool"
            trace("tool", name=name, payload={"error": "unknown tool"}, ok=False)
            return json.dumps({"error": f"Unknown tool '{name}'. Available: {sorted(self.host.tools)}"})

        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            record["status"] = "invalid_args"
            trace("tool", name=name, payload={"error": f"bad JSON: {exc}"}, ok=False)
            return json.dumps({"error": f"Arguments were not valid JSON: {exc}"})
        record["arguments"] = args

        errors = sorted(Draft202012Validator(tool.input_schema).iter_errors(args), key=str)
        if errors:
            record["status"] = "invalid_args"
            msg = "; ".join(e.message for e in errors[:3])
            trace("tool", name=name, payload={"args": args, "error": msg}, ok=False)
            return json.dumps({"error": f"Invalid arguments: {msg}"})

        if not tool.read_only:
            args.pop("dry_run", None)  # host-only control; the model never decides this
            preview = None
            if tool.trusted and tool.supports_dry_run:
                # Preflight: full server-side validation, no write. Humans only approve valid actions.
                check = await self.host.call(name, {**args, "dry_run": True}, allow_retry=True)
                trace("preflight", name=name, latency_ms=check.latency_ms, ok=check.ok,
                      payload={"args": args, "preview": check.data, "error": check.error})
                if not check.ok:
                    record["status"] = "rejected_preflight"
                    return json.dumps({"error": f"Not submitted for approval: {check.error}"})
                preview = check.data
            action = self.store.create_action(session_id, name, args, preview)
            record["status"] = "pending_approval"
            result.pending_actions.append(action.public())
            trace("approval_requested", name=name, payload=action.public(), ok=True)
            return json.dumps({"status": "pending_approval", "action_id": action.id, "preview": preview,
                               "note": "NOT executed. Waiting for a supervisor to approve."})

        outcome = await self.host.call(name, args)
        record["status"] = "ok" if outcome.ok else "error"
        record["attempts"] = outcome.attempts
        trace("tool", name=name, latency_ms=outcome.latency_ms, ok=outcome.ok,
              payload={"args": args, "attempts": outcome.attempts, "error": outcome.error})
        return outcome.for_model()

    async def resolve_action(self, action_id: str, approve: bool, by: str = "local-user") -> dict[str, Any]:
        """Human decision on a pending action. Executes the exact stored payload, at most once."""
        action = self.store.claim_action(action_id, "approved" if approve else "denied", by,
                                         settings.approval_ttl_s)
        turn_id = "approval"
        if not approve:
            self.store.finish_action(action.id, "denied", None)
            self.store.trace(action.session_id, turn_id, "approval_denied", name=action.tool, payload=action.public())
            note = f"The user denied {action.tool} ({action.id[:8]}); it was not executed."
            status, data = "denied", None
        else:
            # Writes are never auto-retried: a timeout may still have committed.
            tool = self.host.tools[action.tool]
            args = {**action.args, "dry_run": False} if tool.supports_dry_run else action.args
            outcome = await self.host.call(action.tool, args, allow_retry=False)
            status = "executed" if outcome.ok else "failed"
            data = outcome.data if outcome.ok else {"error": outcome.error}
            self.store.finish_action(action.id, status, data)
            self.store.trace(action.session_id, turn_id, "approval_executed", name=action.tool,
                             payload={"action_id": action.id, "result": data},
                             latency_ms=outcome.latency_ms, ok=outcome.ok)
            note = f"Approved {action.tool} ({action.id[:8]}) {status}: {json.dumps(data, default=str)}"

        # Record the outcome in the conversation so later turns know what happened.
        messages = self.store.load_messages(action.session_id)
        if messages:
            messages.append({"role": "assistant", "content": f"[system note] {note}"})
            self.store.save_messages(action.session_id, messages)
        return {"action_id": action.id, "tool": action.tool, "status": status, "result": data}


def _describe_pending(a: dict[str, Any]) -> str:
    """Spoken confirmation. Prefer the server's normalized summary, so the user hears the
    canonical IDs (catching a mis-heard accession before anyone approves it)."""
    preview = a.get("preview") or {}
    if preview.get("summary"):
        what = preview["summary"]
    else:
        args = ", ".join(f"{k} {v}" for k, v in a["args"].items() if v is not None)
        what = f"run {a['tool'].split('__')[-1].replace('_', ' ')} with {args}"
    text = f"Ready to {what}. It needs supervisor approval before it runs."
    return f"{text} Note: {preview['warning']}." if preview.get("warning") else text


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
