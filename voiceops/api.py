"""FastAPI app.  Run:  uvicorn voiceops.api:app --reload"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from voiceops.agent.loop import ActionForbidden, Agent
from voiceops.agent.store import ActionError, Store
from voiceops.auth import DEMO_USERS, User, check_decision
from voiceops.config import default_servers, settings
from voiceops.llm import OpenAICompatLLM
from voiceops.mcp_host import MCPHost


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MCP servers are started and stopped in this same task (required by anyio-based transports).
    async with MCPHost(default_servers()) as host:
        app.state.agent = Agent(llm=OpenAICompatLLM(), host=host, store=Store(settings.db_path))
        yield


app = FastAPI(title="VoiceOps", version="0.2.0", lifespan=lifespan)


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


@app.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return user.public()


@app.get("/health")
def health() -> dict:
    a = _agent()
    return {"ok": True, "model": a.llm.model, "tools": len(a.host.tools)}


@app.get("/tools")
def tools() -> list[dict]:
    return [{"name": t.qualified, "read_only": t.read_only, "requires_approval": not t.read_only,
             "description": t.description} for t in _agent().host.tools.values()]


@app.post("/agent")
async def agent(req: AgentRequest, user: User = Depends(current_user)) -> dict:
    return (await _agent().run_turn(req.message, req.session_id, user=user)).to_dict()


@app.get("/actions")
def list_actions(status: str = "pending", user: User = Depends(current_user)) -> list[dict]:
    """The approval queue: actions this user may approve, plus their own requests."""
    out = []
    for a in _agent().store.list_actions(status or None):
        can_approve = check_decision(a.tool, a.requested_by, user, approve=True) is None
        if can_approve or a.requested_by == user.id:
            out.append({**a.public(), "can_approve": can_approve})
    return out


@app.post("/actions/{action_id}")
async def decide(action_id: str, body: Decision, user: User = Depends(current_user)) -> dict:
    try:
        return await _agent().resolve_action(action_id, body.decision == "approve", by=user)
    except ActionForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/actions/{action_id}")
def get_action(action_id: str) -> dict:
    row = _agent().store.get_action(action_id)
    if not row:
        raise HTTPException(404, "not found")
    return row


@app.get("/sessions/{session_id}/trace")
def trace(session_id: str) -> list[dict]:
    return _agent().store.get_trace(session_id)


async def _transcribe(audio: UploadFile) -> dict:
    from voiceops.stt import transcribe_file

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        path = tmp.name
    try:
        # Whisper is blocking (GPU/CPU-bound): keep it off the event loop.
        return await asyncio.to_thread(transcribe_file, path)
    finally:
        os.remove(path)


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    return await _transcribe(audio)


@app.post("/voice")
async def voice(audio: UploadFile = File(...), session_id: str | None = Form(None),
                user: User = Depends(current_user)) -> dict:
    stt = await _transcribe(audio)
    if not stt["text"]:
        return {"transcript": stt, "turn": None}
    turn = await _agent().run_turn(stt["text"], session_id, user=user)
    return {"transcript": stt, "turn": turn.to_dict()}
