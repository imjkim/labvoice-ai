"""MCP host: launches MCP servers, discovers their tools, and calls them.

The LLM only ever sees tool *schemas*. Execution, timeouts, retries and
approval policy all live here and in the agent loop, in trusted code.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.types import ToolAnnotations

from voiceops.config import ROOT, ServerSpec, settings

# Tool-level errors worth retrying. "Customer not found" is not; a timeout is.
TRANSIENT = re.compile(r"timeout|timed out|temporar|unavailable|rate.?limit|\b50[234]\b", re.I)

SEP = "__"  # qualified tool name: <server>__<tool>  (valid OpenAI function name chars)


@dataclass
class HostedTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotations | None
    trusted: bool

    @property
    def qualified(self) -> str:
        return f"{self.server}{SEP}{self.name}"

    @property
    def read_only(self) -> bool:
        """Honor read_only_hint only from trusted servers. Missing hint => not read-only."""
        return bool(self.trusted and self.annotations and self.annotations.read_only_hint is True)

    @property
    def supports_dry_run(self) -> bool:
        return "dry_run" in (self.input_schema or {}).get("properties", {})

    def model_schema(self) -> dict[str, Any]:
        """The schema the model sees. dry_run is a host-only control, so it is hidden."""
        schema = dict(self.input_schema or {"type": "object", "properties": {}})
        if self.supports_dry_run:
            schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "dry_run"}
        return schema

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.qualified, "description": self.description, "parameters": self.model_schema()},
        }


@dataclass
class ToolOutcome:
    ok: bool
    data: Any
    error: str | None
    latency_ms: float
    attempts: int

    def for_model(self) -> str:
        """What gets fed back to the LLM as the tool message content."""
        if self.ok:
            return json.dumps(self.data, default=str)
        return json.dumps({"error": self.error, "attempts": self.attempts})


class MCPHost:
    def __init__(self, specs: list[ServerSpec]):
        self.specs = specs
        self.tools: dict[str, HostedTool] = {}
        self._clients: dict[str, Client] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "MCPHost":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        for spec in self.specs:
            params = StdioServerParameters(
                command=spec.command,
                args=spec.args,
                env={**os.environ, **spec.env},
                cwd=str(ROOT),
            )
            client = await self._stack.enter_async_context(Client(params))
            self._clients[spec.name] = client
            listed = await client.list_tools()
            for t in listed.tools:
                tool = HostedTool(
                    server=spec.name,
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.input_schema,
                    annotations=t.annotations,
                    trusted=spec.trusted,
                )
                self.tools[tool.qualified] = tool

    async def close(self) -> None:
        await self._stack.aclose()

    def openai_tools(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self.tools.values()]

    async def call(self, qualified: str, args: dict[str, Any], *, allow_retry: bool | None = None) -> ToolOutcome:
        """Call a tool with a timeout. Retries transient failures once with backoff,
        and only for read-only tools by default: never blindly retry a write."""
        tool = self.tools[qualified]
        retries = 1 if (tool.read_only if allow_retry is None else allow_retry) else 0
        client = self._clients[tool.server]
        start = time.perf_counter()
        error = "unknown error"
        for attempt in range(1, retries + 2):
            try:
                result = await asyncio.wait_for(
                    client.call_tool(tool.name, args), timeout=settings.tool_timeout_s
                )
                if not result.is_error:
                    data = result.structured_content
                    if data is None:
                        data = "\n".join(getattr(c, "text", "") for c in result.content)
                    return ToolOutcome(True, data, None, _ms(start), attempt)
                error = "\n".join(getattr(c, "text", "") for c in result.content) or "tool error"
                if not TRANSIENT.search(error):
                    return ToolOutcome(False, None, error, _ms(start), attempt)
            except asyncio.TimeoutError:
                error = f"timed out after {settings.tool_timeout_s}s"
            except Exception as exc:  # transport-level failure
                error = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                await asyncio.sleep(0.25 * attempt)
        return ToolOutcome(False, None, error, _ms(start), retries + 1)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
