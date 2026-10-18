"""FastAPI app.  Run:  uvicorn voiceops.api:app --reload"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from voiceops.agent.loop import Agent
from voiceops.agent.store import ActionError, Store
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
    approver: str = "local-user"


def _agent() -> Agent:
    return app.state.agent


@app.get("/health")
def health() -> dict:
    a = _agent()
    return {"ok": True, "model": a.llm.model, "tools": len(a.host.tools)}


@app.get("/tools")
def tools() -> list[dict]:
    return [{"name": t.qualified, "read_only": t.read_only, "requires_approval": not t.read_only,
             "description": t.description} for t in _agent().host.tools.values()]


@app.post("/agent")
async def agent(req: AgentRequest) -> dict:
    return (await _agent().run_turn(req.message, req.session_id)).to_dict()


@app.post("/actions/{action_id}")
async def decide(action_id: str, body: Decision) -> dict:
    try:
        return await _agent().resolve_action(action_id, body.decision == "approve", body.approver)
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
async def voice(audio: UploadFile = File(...), session_id: str | None = Form(None)) -> dict:
    stt = await _transcribe(audio)
    if not stt["text"]:
        return {"transcript": stt, "turn": None}
    turn = await _agent().run_turn(stt["text"], session_id)
    return {"transcript": stt, "turn": turn.to_dict()}
