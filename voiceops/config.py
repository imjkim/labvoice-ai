"""Settings from environment / .env. Kept dependency-light on purpose."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class ServerSpec:
    """An MCP server the host launches over stdio."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # Only trusted servers' read_only_hint is honored. Untrusted => every tool needs approval.
    trusted: bool = False


@dataclass(frozen=True)
class Settings:
    # Any OpenAI-compatible chat-completions endpoint. Defaults to local Ollama.
    llm_base_url: str = _env("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    llm_api_key: str = _env("LLM_API_KEY", "ollama")
    llm_model: str = _env("LLM_MODEL", "qwen3:4b")
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0"))

    max_agent_steps: int = int(_env("MAX_AGENT_STEPS", "6"))
    tool_timeout_s: float = float(_env("TOOL_TIMEOUT_S", "10"))
    approval_ttl_s: int = int(_env("APPROVAL_TTL_S", "900"))

    db_path: str = _env("VOICEOPS_DB_PATH", str(ROOT / "data" / "voiceops.db"))
    lims_db_path: str = _env("LIMS_DB_PATH", str(ROOT / "data" / "lims.db"))
    lims_fault_rate: str = _env("LIMS_FAULT_RATE", "0")

    whisper_model: str = _env("WHISPER_MODEL", "small")
    whisper_device: str = _env("WHISPER_DEVICE", "cuda")
    whisper_compute_type: str = _env("WHISPER_COMPUTE_TYPE", "int8_float16")


settings = Settings()


def default_servers(lims_db_path: str | None = None, fault_rate: str | None = None) -> list[ServerSpec]:
    return [
        ServerSpec(
            name="lims",
            command=sys.executable,
            args=["-m", "servers.lims_server"],
            env={
                "LIMS_DB_PATH": lims_db_path or settings.lims_db_path,
                "LIMS_FAULT_RATE": fault_rate if fault_rate is not None else settings.lims_fault_rate,
            },
            trusted=True,  # first-party server we wrote and reviewed
        ),
    ]
