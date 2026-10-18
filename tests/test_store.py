"""Regression test: every SQLite connection must be closed, not left for the garbage collector.

Leaked handles are invisible on Linux (open files can be deleted) but break Windows,
where an open handle makes the file undeletable. This test catches it on both.
"""
from __future__ import annotations

import gc
import os
import tempfile
from pathlib import Path

from voiceops.agent.store import Store


def _open_handles(path: str) -> int:
    """Count this process's open file descriptors pointing at `path` (POSIX via /proc)."""
    fd_dir = Path("/proc/self/fd")
    target = os.path.realpath(path)
    count = 0
    for fd in fd_dir.iterdir():
        try:
            if os.path.realpath(os.readlink(fd)) == target:
                count += 1
        except OSError:
            pass
    return count


def test_store_closes_every_connection():
    gc.disable()  # leaked connections sit in reference cycles; don't let the GC hide them
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = str(Path(d) / "agent.db")
            store = Store(path)
            action = store.create_action("s1", "lims__flag_specimen", {"accession": "S26-00312"})
            store.claim_action(action.id, "approved", "tester", ttl_s=60)
            store.finish_action(action.id, "executed", {"ok": True})
            store.save_messages("s1", [{"role": "user", "content": "hi"}])
            store.load_messages("s1")
            store.trace("s1", "t1", "llm", latency_ms=1.0, ok=True)
            store.get_trace("s1")

            if os.name == "nt":
                os.remove(path)  # raises PermissionError if any handle is still open
            elif Path("/proc/self/fd").exists():
                assert _open_handles(path) == 0, "Store leaked SQLite connections"
    finally:
        gc.enable()
