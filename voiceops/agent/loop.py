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
import re
import time
import uuid
from datetime import datetime
from dataclasses import asdict, dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from voiceops.agent.store import ActionError, Store
from voiceops.auth import User, approver_roles, check_decision
from voiceops.config import settings
from voiceops.llm import LLM, AssistantMessage
from voiceops.mcp_host import HOST_ONLY_PARAMS, MCPHost

_YY = f"S{datetime.now().year % 100:02d}"

SYSTEM_PROMPT = f"""You are VoiceOps, a hands-free assistant for histology lab staff working at the bench.

- Use the LIMS tools for anything about specimens, stains, instruments or QC. Never guess lab data.
- Staff usually say only the last digits of an accession ("287"). That is enough to act on: pass exactly
  what they said to the tool (e.g. "287"). Never add or guess a prefix, and don't ask for the full number.
- When you answer, use the full accession from the tool result (e.g. {_YY}-00287).
- get_specimen returns a `headline`: base your answer on it, including any problem it reports.
- An instrument is down unless its status is online (maintenance, qc_hold and offline are all down).
- Your replies are spoken aloud: 1-3 short sentences, plain text, no markdown, no lists.
- Tools that change the LIMS (ordering stains, flagging specimens, changing instrument status) are held for
  approval by an authorized person (supervisor, lead tech or pathologist, depending on the action).
  Never say an action was done unless a tool result confirms it.
- If a tool returns an error, say briefly what failed. If a request is ambiguous, ask one short question.
- You don't interpret results or make diagnoses: for clinical questions, say that's the pathologist's call.
  Politely decline requests unrelated to the lab.
- You never have patient names or identifiers, and must not ask for them.
"""


# ---- grounding guard --------------------------------------------------------------------------
# Observed failure: with no tool call at all, the model answered "S26-00312 is overdue and blocked
# by a labeling discrepancy" (false). A prompt rule can't make this impossible; this check can.
_IDS = re.compile(r"\bS\d{2}-\d{5}\b|\b[A-Z]{2,3}-\d{2}\b")  # accessions, instrument IDs
_LAB_FACTS = re.compile(
    r"\b(overdue|blocked|flagged|qc|quality control|stain(?:ed|ing)?|processing|signed out|on hold|"
    r"maintenance|offline|in progress|slides? ready|under review|embedded|sectioned|grossed)\b", re.I)

GROUNDING_NUDGE = (
    "[grounding check] Your last reply stated lab facts (specimens, stains, instruments or QC) that no tool "
    "result in this turn supports. If the user asked about lab data, call the right read tool now and answer "
    "only from its result. If they didn't, reply without stating any lab facts.")
UNGROUNDED_FALLBACK = "I couldn't confirm that in the LIMS, so I won't guess. Could you ask again?"


def grounding_problem(answer: str, evidence: list[str]) -> str | None:
    """Return why `answer` isn't backed by this turn's tool results, or None if it is.

    Two checks: (1) every accession or instrument ID named in the answer must appear in a tool
    result from this turn; (2) an answer that states lab facts needs at least one tool result.
    Facts from earlier turns don't count: lab state changes, so each answer re-reads it."""
    blob = "\n".join(evidence)
    missing = sorted({i for i in _IDS.findall(answer) if i not in blob})
    if missing:
        return f"names {', '.join(missing)}, which no tool result in this turn contains"
    if not evidence and _LAB_FACTS.search(answer):
        return "states lab facts without calling any tool"
    return None


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

    async def run_turn(self, user_text: str, session_id: str | None = None,
                       user: User | None = None) -> TurnResult:
        session_id = session_id or str(uuid.uuid4())
        turn_id = str(uuid.uuid4())[:8]
        t_turn = time.perf_counter()
        result = TurnResult(session_id=session_id, turn_id=turn_id, answer="")

        def trace(kind: str, **kw: Any) -> None:
            result.trace.append(self.store.trace(session_id, turn_id, kind, **kw))

        messages = self.store.load_messages(session_id) or [{"role": "system", "content": self.system_prompt}]
        messages.append({"role": "user", "content": user_text})
        trace("user", payload={"text": user_text, "user": user.public() if user else None})
        tools = self.host.openai_tools()

        evidence: list[str] = []         # every tool result text seen this turn
        nudge: dict[str, str] | None = None  # the one grounding re-prompt, kept out of saved history
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
                problem = grounding_problem(reply.content, evidence)
                if problem and nudge is None:
                    # The model answered about lab data it never looked up. Don't keep that answer;
                    # tell it what's wrong and give it one chance to check.
                    trace("grounding_retry", payload={"answer": reply.content, "problem": problem}, ok=False)
                    nudge = {"role": "system", "content": GROUNDING_NUDGE}
                    messages.append(nudge)
                    continue
                if problem:
                    trace("grounding_blocked", payload={"answer": reply.content, "problem": problem}, ok=False)
                    result.answer = UNGROUNDED_FALLBACK
                else:
                    result.answer = reply.content
                messages.append({"role": "assistant", "content": result.answer})
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
                content = await self._handle_call(call.name, call.arguments, session_id, result, trace, user)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                evidence.append(content)

            if result.pending_actions:
                # Deterministic, not model-written: the user hears exactly what is awaiting approval.
                result.answer = " ".join(_describe_pending(a) for a in result.pending_actions)
                messages.append({"role": "assistant", "content": result.answer})
                break
        else:
            result.answer = "I couldn't finish that within my step limit. Could you narrow the request?"
            messages.append({"role": "assistant", "content": result.answer})

        if nudge is not None:
            messages.remove(nudge)  # a per-turn correction, not part of the conversation
        self.store.save_messages(session_id, messages)
        result.latency_ms = _ms(t_turn)
        trace("answer", payload=result.answer, latency_ms=result.latency_ms, ok=True)
        return result

    async def _handle_call(self, name: str, raw_args: str, session_id: str, result: TurnResult, trace,
                           user: User | None = None) -> str:
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
        for param in HOST_ONLY_PARAMS & set(args):
            args.pop(param)  # dry_run / actor are the host's to set, never the model's
        record["arguments"] = args

        errors = sorted(Draft202012Validator(tool.input_schema).iter_errors(args), key=str)
        if errors:
            record["status"] = "invalid_args"
            msg = "; ".join(e.message for e in errors[:3])
            trace("tool", name=name, payload={"args": args, "error": msg}, ok=False)
            return json.dumps({"error": f"Invalid arguments: {msg}"})

        if not tool.read_only:
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
            action = self.store.create_action(session_id, name, args, preview,
                                              requested_by=user.id if user else None,
                                              approvers=sorted(approver_roles(name)))
            record["status"] = "pending_approval"
            result.pending_actions.append(action.public())
            trace("approval_requested", name=name, payload=action.public(), ok=True)
            return json.dumps({"status": "pending_approval", "action_id": action.id, "preview": preview,
                               "note": "NOT executed. Waiting for approval by: " + ", ".join(action.approvers or [])})

        outcome = await self.host.call(name, args)
        record["status"] = "ok" if outcome.ok else "error"
        record["attempts"] = outcome.attempts
        trace("tool", name=name, latency_ms=outcome.latency_ms, ok=outcome.ok,
              payload={"args": args, "attempts": outcome.attempts, "error": outcome.error})
        return outcome.for_model()

    async def resolve_action(self, action_id: str, approve: bool, by: User) -> dict[str, Any]:
        """A human decision on a pending action. Authorization is checked BEFORE the action is
        claimed, so an unauthorized attempt can't consume it. Executes the exact stored payload,
        at most once, with the approver recorded in the system of record."""
        action = self.store.load_action(action_id)
        if action is None:
            raise ActionError("action not found")
        reason = check_decision(action.tool, action.requested_by, by, approve)
        if reason:
            self.store.trace(action.session_id, "approval", "approval_forbidden", name=action.tool,
                             payload={"action_id": action.id, "by": by.public(), "approve": approve, "reason": reason},
                             ok=False)
            raise ActionForbidden(reason)

        action = self.store.claim_action(action_id, "approved" if approve else "denied", by.id,
                                         settings.approval_ttl_s)
        turn_id = "approval"
        if not approve:
            withdrawn = by.id == action.requested_by
            self.store.finish_action(action.id, "denied", {"withdrawn": withdrawn})
            self.store.trace(action.session_id, turn_id, "approval_denied", name=action.tool,
                             payload={**action.public(), "by": by.public(), "withdrawn": withdrawn})
            verb = "withdrew" if withdrawn else "denied"
            note = f"{by.name} {verb} {action.tool} ({action.id[:8]}); it was not executed."
            status, data = "denied", None
        else:
            tool = self.host.tools[action.tool]
            args = dict(action.args)
            if tool.supports_dry_run:
                args["dry_run"] = False
            if tool.accepts("actor"):
                args["actor"] = f"{by.name} ({by.role})"  # lands in the LIMS audit trail
            # Writes are never auto-retried: a timeout may still have committed.
            outcome = await self.host.call(action.tool, args, allow_retry=False)
            status = "executed" if outcome.ok else "failed"
            data = outcome.data if outcome.ok else {"error": outcome.error}
            self.store.finish_action(action.id, status, data)
            self.store.trace(action.session_id, turn_id, "approval_executed", name=action.tool,
                             payload={"action_id": action.id, "by": by.public(), "result": data},
                             latency_ms=outcome.latency_ms, ok=outcome.ok)
            note = f"{by.name} approved {action.tool} ({action.id[:8]}), {status}: {json.dumps(data, default=str)}"

        # Record the outcome in the conversation so later turns know what happened.
        messages = self.store.load_messages(action.session_id)
        if messages:
            messages.append({"role": "assistant", "content": f"[system note] {note}"})
            self.store.save_messages(action.session_id, messages)
        return {"action_id": action.id, "tool": action.tool, "status": status, "result": data,
                "decided_by": by.public()}


class ActionForbidden(ActionError):
    """The user may not make this decision on this action."""


def _describe_pending(a: dict[str, Any]) -> str:
    """Spoken confirmation. Prefer the server's normalized summary, so the user hears the
    canonical IDs (catching a mis-heard accession before anyone approves it)."""
    preview = a.get("preview") or {}
    if preview.get("summary"):
        what = preview["summary"]
    else:
        args = ", ".join(f"{k} {v}" for k, v in a["args"].items() if v is not None)
        what = f"run {a['tool'].split('__')[-1].replace('_', ' ')} with {args}"
    roles = a.get("approvers") or ["supervisor"]
    who = " or ".join(r.replace("_", " ") for r in roles)
    text = f"Ready to {what}. It needs approval from a {who} before it runs."
    return f"{text} Note: {preview['warning']}." if preview.get("warning") else text


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
