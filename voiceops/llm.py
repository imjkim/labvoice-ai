"""LLM clients behind one small interface.

OpenAICompatLLM talks to any OpenAI-compatible /chat/completions endpoint:
Ollama (local, free), vLLM, LM Studio, or a hosted provider. Swapping the
model is a .env change, which also makes model-vs-model evals trivial.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from voiceops.config import settings

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model; parsed (and validated) by the agent


@dataclass
class AssistantMessage:
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class LLM(Protocol):
    model: str

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantMessage: ...


class OpenAICompatLLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        from openai import AsyncOpenAI  # lazy: tests don't need the SDK

        self.model = model or settings.llm_model
        self._client = AsyncOpenAI(base_url=base_url or settings.llm_base_url,
                                   api_key=api_key or settings.llm_api_key)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantMessage:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            temperature=settings.llm_temperature,
        )
        msg = resp.choices[0].message
        calls = [
            ToolCallRequest(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
            for c in (msg.tool_calls or [])
        ]
        usage = {}
        if resp.usage:
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        # Reasoning models served via Ollama may inline <think> blocks; never speak those aloud.
        content = _THINK.sub("", msg.content or "").strip()
        return AssistantMessage(content=content, tool_calls=calls, usage=usage)


class ScriptedLLM:
    """Deterministic stand-in for tests: returns queued responses in order and
    records every request so tests can assert on what the model was shown."""

    model = "scripted"

    def __init__(self, responses: list[AssistantMessage]):
        self._responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantMessage:
        self.requests.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of responses")
        return self._responses.pop(0)
