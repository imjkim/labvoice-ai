"""SQLite persistence: conversation state, pending actions (HITL), and traces.

Pending actions are the security boundary. The model proposes; this table
records exactly what was proposed; a human approves *that exact payload*;
an atomic status transition guarantees it executes at most once.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending | approved | denied | executed | failed | expired
    preview TEXT, created_at REAL NOT NULL, resolved_at REAL, resolved_by TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
    kind TEXT NOT NULL, name TEXT, payload TEXT, latency_ms REAL, ok INTEGER, ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_session ON traces(session_id, id);
"""


@dataclass
class Action:
    id: str
    session_id: str
    tool: str
    args: dict[str, Any]
    status: str
    created_at: float
    preview: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {"action_id": self.id, "tool": self.tool, "args": self.args, "status": self.status,
                "preview": self.preview}


class ActionError(Exception):
    pass


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, and ALWAYS close.
        (`with sqlite3.connect(...)` alone commits but leaves the connection open, which
        leaks a handle per call and makes the file undeletable on Windows.)"""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- sessions ----------------------------------------------------------
    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT messages FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row["messages"]) if row else []

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (id, messages, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
                (session_id, json.dumps(messages), time.time()),
            )

    # ---- actions (human-in-the-loop) ---------------------------------------
    def create_action(self, session_id: str, tool: str, args: dict[str, Any],
                      preview: dict[str, Any] | None = None) -> Action:
        action = Action(str(uuid.uuid4()), session_id, tool, args, "pending", time.time(), preview)
        with self._conn() as c:
            c.execute(
                "INSERT INTO actions (id, session_id, tool, args, preview, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (action.id, session_id, tool, json.dumps(args), json.dumps(preview, default=str), "pending",
                 action.created_at),
            )
        return action

    def claim_action(self, action_id: str, decision: str, by: str, ttl_s: int) -> Action:
        """Atomically move pending -> approved|denied. Raises if not claimable.
        Two concurrent approvals: exactly one wins (UPDATE ... WHERE status='pending')."""
        assert decision in ("approved", "denied")
        with self._conn() as c:
            row = c.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                raise ActionError("action not found")
            if row["status"] == "pending" and time.time() - row["created_at"] > ttl_s:
                c.execute("UPDATE actions SET status = 'expired' WHERE id = ? AND status = 'pending'", (action_id,))
                raise ActionError("action expired; ask the agent again")
            cur = c.execute(
                "UPDATE actions SET status = ?, resolved_at = ?, resolved_by = ? WHERE id = ? AND status = 'pending'",
                (decision, time.time(), by, action_id),
            )
            if cur.rowcount != 1:
                raise ActionError(f"action already {row['status']}")
        return Action(row["id"], row["session_id"], row["tool"], json.loads(row["args"]), decision,
                      row["created_at"], json.loads(row["preview"]) if row["preview"] else None)

    def finish_action(self, action_id: str, status: str, result: Any) -> None:
        with self._conn() as c:
            c.execute("UPDATE actions SET status = ?, result = ? WHERE id = ?",
                      (status, json.dumps(result, default=str), action_id))

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return dict(row) if row else None

    # ---- traces -------------------------------------------------------------
    def trace(self, session_id: str, turn_id: str, kind: str, name: str | None = None,
              payload: Any = None, latency_ms: float | None = None, ok: bool | None = None) -> dict[str, Any]:
        event = {"turn_id": turn_id, "kind": kind, "name": name, "payload": payload,
                 "latency_ms": latency_ms, "ok": ok, "ts": time.time()}
        with self._conn() as c:
            c.execute(
                "INSERT INTO traces (session_id, turn_id, kind, name, payload, latency_ms, ok, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, turn_id, kind, name, json.dumps(payload, default=str), latency_ms,
                 None if ok is None else int(ok), event["ts"]),
            )
        return event

    def get_trace(self, session_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM traces WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"]) if r["payload"] else None} for r in rows]
