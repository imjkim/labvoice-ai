"""FastAPI app: the JSON API under /api, and the bench web client at /.

Run:  uvicorn voiceops.api:app      then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from voiceops.agent.loop import ActionForbidden, Agent
from voiceops.agent.store import ActionError, Store
from voiceops.auth import APPROVAL_POLICY, DEFAULT_APPROVERS, DEMO_USERS, User, check_decision
from voiceops.config import ROOT, default_servers, settings
from voiceops.llm import OpenAICompatLLM
from voiceops.mcp_host import MCPHost

WEB_DIR = ROOT / "web"
log = logging.getLogger("uvicorn.error")  # shows up in the uvicorn console


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MCP servers are started and stopped in this same task (required by anyio-based transports).
    async with MCPHost(default_servers()) as host:
        llm = OpenAICompatLLM()
        app.state.agent = Agent(llm=llm, host=host, store=Store(settings.db_path))
        warm = asyncio.create_task(_warm_up(llm))  # don't block startup on it
        yield
        warm.cancel()


async def _warm_up(llm: OpenAICompatLLM) -> None:
    try:
        ms = await llm.warm_up()
        log.info("Model %s warmed up in %.0f ms", llm.model, ms)
    except Exception as exc:  # Ollama not running yet: the first real request will report it
        log.warning("Model warm-up skipped: %s", exc)


app = FastAPI(title="LabVoice AI", version="0.4.0", lifespan=lifespan)
api = APIRouter(prefix="/api")


class AgentRequest(BaseModel):
    message: str
    session_id: str | None = None


class Decision(BaseModel):
    decision: Literal["approve", "deny"]


def _agent() -> Agent:
    return app.state.agent


def current_user(x_user: str = Header(..., description=f"Demo identity, one of: {', '.join(DEMO_USERS)}")) -> User:
    """Demo identity from the X-User header. In a real deployment this comes from SSO."""
    user = DEMO_USERS.get(x_user.strip().lower())
    if user is None:
        raise HTTPException(401, f"Unknown user '{x_user}'. Demo users: {', '.join(DEMO_USERS)}")
    return user


@api.get("/health")
def health() -> dict:
    from voiceops.stt import device

    a = _agent()
    return {"ok": True, "model": a.llm.model, "tools": len(a.host.tools), "stt_device": device()}


@api.get("/users")
def users() -> list[dict]:
    """The demo directory, for the client's identity switcher."""
    return [u.public() for u in DEMO_USERS.values()]


@api.get("/policy")
def policy() -> dict:
    """Who may approve which write tool (shown in the client next to each request)."""
    return {"default": sorted(DEFAULT_APPROVERS), "tools": {t: sorted(r) for t, r in APPROVAL_POLICY.items()}}


@api.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return user.public()


@api.get("/tools")
def tools() -> list[dict]:
    return [{"name": t.qualified, "read_only": t.read_only, "requires_approval": not t.read_only,
             "description": t.description} for t in _agent().host.tools.values()]


@api.post("/agent")
async def agent(req: AgentRequest, user: User = Depends(current_user)) -> dict:
    return (await _agent().run_turn(req.message, req.session_id, user=user)).to_dict()


@api.get("/actions")
def list_actions(status: str = "pending", user: User = Depends(current_user)) -> list[dict]:
    """The approval queue: actions this user may approve, plus their own requests."""
    out = []
    for a in _agent().store.list_actions(status or None):
        can_approve = check_decision(a.tool, a.requested_by, user, approve=True) is None
        if can_approve or a.requested_by == user.id:
            requester = DEMO_USERS.get(a.requested_by or "")
            out.append({**a.public(), "can_approve": can_approve,
                        "requested_by_name": requester.name if requester else None})
    return out


@api.post("/actions/{action_id}")
async def decide(action_id: str, body: Decision, user: User = Depends(current_user)) -> dict:
    try:
        return await _agent().resolve_action(action_id, body.decision == "approve", by=user)
    except ActionForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.get("/actions/{action_id}")
def get_action(action_id: str) -> dict:
    row = _agent().store.get_action(action_id)
    if not row:
        raise HTTPException(404, "not found")
    return row


@api.get("/sessions/{session_id}/trace")
def trace(session_id: str) -> list[dict]:
    return _agent().store.get_trace(session_id)


async def _transcribe(audio: UploadFile) -> dict:
    from voiceops.stt import transcribe_file

    # Browsers record webm/ogg (Chrome, Firefox) or mp4 (Safari); Whisper decodes all of them.
    suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty recording. Hold the button while you speak.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        # Whisper is blocking (GPU/CPU-bound): keep it off the event loop.
        return await asyncio.to_thread(transcribe_file, path)
    finally:
        os.remove(path)


@api.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    return await _transcribe(audio)


@api.post("/voice")
async def voice(audio: UploadFile = File(...), session_id: str | None = Form(None),
                user: User = Depends(current_user)) -> dict:
    """One spoken turn: speech -> text -> agent. Returns per-stage timings for the latency budget."""
    stt = await _transcribe(audio)
    if not stt["text"]:
        return {"transcript": stt, "turn": None, "timings": {"stt_ms": stt["latency_ms"]}}
    t0 = time.perf_counter()
    turn = await _agent().run_turn(stt["text"], session_id, user=user)
    return {"transcript": stt, "turn": turn.to_dict(),
            "timings": {"stt_ms": stt["latency_ms"], "agent_ms": round((time.perf_counter() - t0) * 1000, 1)}}


app.include_router(api)

# Python's mimetypes reads the Windows registry, which often maps .js to text/plain. Browsers refuse
# to run ES modules served with that type, so the page would load blank. Pin the types we serve.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Mounted last so /api routes take precedence. html=True serves web/index.html at "/".
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
