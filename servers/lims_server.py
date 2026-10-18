"""Mock histology LIMS exposed as an MCP server (MCP Python SDK 2.x).

Run standalone:  python -m servers.lims_server
The agent host launches it as a subprocess over stdio.

Design rules (see DECISIONS.md):
- Data minimization at the tool boundary: no tool ever returns patient name,
  DOB or MRN. The model can't leak what it never receives.
- Tools are tolerant of how people *say* IDs ("S 26 312", "ihc stainer one")
  and strict about what they *do* (writes validate everything, then raise
  ToolError with a message the model can act on).
- Every tool declares MCP annotations; the host treats them as hints and
  sends anything not read-only through human approval.
- Every write tool supports dry_run: full validation plus a preview, no write.
  The host preflights with it so humans only ever approve valid actions.
- Every write tool takes `actor`, set by the host to the approving human, so the
  LIMS audit trail says who approved each change. The model never sees either.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from servers.synth import STAGES, STAINS, generate

DB_PATH = Path(os.environ.get("LIMS_DB_PATH", "data/lims.db"))
FAULT_RATE = float(os.environ.get("LIMS_FAULT_RATE", "0"))  # resilience demos/evals

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (id TEXT PRIMARY KEY, name TEXT, dob TEXT, mrn TEXT);
CREATE TABLE IF NOT EXISTS specimens (
    accession TEXT PRIMARY KEY, patient_id TEXT REFERENCES patients(id), type TEXT, site TEXT,
    priority TEXT, status TEXT, blocks INTEGER, received_at TEXT, due_at TEXT, pathologist TEXT, flag TEXT);
CREATE TABLE IF NOT EXISTS stain_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, accession TEXT REFERENCES specimens(accession), block TEXT,
    stain TEXT, status TEXT, instrument_id TEXT, ordered_at TEXT, ordered_by TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS instruments (id TEXT PRIMARY KEY, name TEXT, type TEXT, status TEXT, location TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS qc_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT, run_at TEXT, result TEXT, notes TEXT, performed_by TEXT);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, accession TEXT, ts TEXT, event TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS instrument_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT, ts TEXT, change TEXT, actor TEXT);
"""

# Columns the model may see. Patient identifiers are deliberately absent.
SPECIMEN_COLS = "accession, type, site, priority, status, blocks, received_at, due_at, pathologist, flag"

mcp = MCPServer("lims", instructions="Histology lab LIMS: specimens, blocks, stain orders, instruments and QC.")


# ---------------------------------------------------------------- plumbing ---
@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, ALWAYS close (see store.py for why)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM specimens").fetchone()[0]:
            return
        data = generate(seed=int(os.environ.get("LIMS_SEED", "7")))
        for table, rows in data.items():
            if rows:
                cols = list(rows[0])
                c.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})", rows)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def _maybe_fault() -> None:
    if FAULT_RATE and random.random() < FAULT_RATE:
        raise ToolError("LIMS upstream timeout (injected fault)")


def normalize_accession(text: str, year: int | None = None) -> str:
    """Accept how people actually say/type accessions; return canonical 'S26-00312'.

    'S26-00312', 's 26 312', '26-312' -> year 26, seq 312   (two digit groups)
    'S26312', '2600312'                -> first two digits are the year
    '312', '00312'                     -> sequence only: assume the current year
    Spoken numbers ("three twelve") are the STT layer's job, not this function's.
    """
    yy = (year or datetime.now(timezone.utc).year) % 100
    upper = text.strip().upper()
    groups = re.findall(r"\d+", upper)
    if re.search(r"[^S0-9\s\-/.]", upper) or not groups or len(groups) > 2:
        raise ToolError(f"'{text}' is not an accession number. Expected a form like S{yy:02d}-00312.")
    if len(groups) == 2:
        y, seq = groups
    elif upper.startswith("S") or len(groups[0]) > 5:
        y, seq = groups[0][:2], groups[0][2:]
    else:
        y, seq = str(yy), groups[0]
    if len(y) != 2 or not seq or len(seq) > 5:
        raise ToolError(f"'{text}' is not an accession number. Expected a form like S{yy:02d}-00312.")
    return f"S{y}-{int(seq):05d}"


def _specimen(c: sqlite3.Connection, accession: str) -> dict[str, Any]:
    acc = normalize_accession(accession)
    row = c.execute(f"SELECT {SPECIMEN_COLS} FROM specimens WHERE accession = ?", (acc,)).fetchone()
    if not row:
        raise ToolError(f"No specimen {acc}. Ask the user to repeat the accession number.")
    return dict(row)


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9", "first": "1", "second": "2"}


def _spoken_key(text: str) -> str:
    """'IHC stainer one' -> 'ihcstainer1'; 'IHC-01' -> 'ihc01'. Voice input says numbers as words."""
    words = [w for w in re.findall(r"[a-z0-9&]+", text.lower()) if w not in ("the", "a", "an", "number", "no")]
    return "".join(_NUMBER_WORDS.get(w, w) for w in words).replace("&", "")


def _instrument(c: sqlite3.Connection, ref: str) -> dict[str, Any]:
    """Match an ID ('IHC-01', 'ihc 1'), a spoken name ('IHC stainer one'), or a name fragment
    ('H&E stainer'). Ambiguity is an error that lists the options."""
    rows = [dict(r) for r in c.execute("SELECT * FROM instruments")]
    key = _spoken_key(ref)

    def id_key(iid: str) -> str:  # 'IHC-01' -> 'ihc1' so 'ihc 1' and 'ihc01' both match
        m = re.fullmatch(r"([a-z]+)0*(\d+)", re.sub(r"[^a-z0-9]", "", iid.lower()))
        return f"{m.group(1)}{m.group(2)}" if m else iid.lower()

    loose = re.sub(r"^([a-z]+)0*(\d+)$", r"\1\2", key)
    hits = [r for r in rows if id_key(r["id"]) == loose] or \
           [r for r in rows if _spoken_key(r["name"]) == key] or \
           [r for r in rows if key and (key in _spoken_key(r["name"]) or key in _spoken_key(r["type"]))]
    if not hits:
        known = ", ".join(f"{r['id']} ({r['name']})" for r in rows)
        raise ToolError(f"No instrument matches '{ref}'. Known: {known}")
    if len(hits) > 1:
        options = ", ".join(f"{r['id']} ({r['name']})" for r in hits)
        raise ToolError(f"'{ref}' is ambiguous: {options}")
    return hits[0]


def _block_labels(n: int) -> list[str]:
    return [f"A{i}" for i in range(1, n + 1)]


def _actor(actor: str | None) -> str:
    """Audit-trail attribution for a write: the approving human, via VoiceOps."""
    return f"{actor} via VoiceOps" if actor else "VoiceOps (unattributed)"


def _event(c: sqlite3.Connection, accession: str, event: str, actor: str) -> None:
    c.execute("INSERT INTO events (accession, ts, event, actor) VALUES (?, ?, ?, ?)", (accession, _now(), event, actor))


# ------------------------------------------------------------------ reads ---
@mcp.tool(annotations=READ)
def get_specimen(accession: str) -> dict[str, Any]:
    """Get one specimen by accession number (e.g. S26-00312): a one-line `headline` to read back,
    workflow status, priority, due time and whether it's overdue, blocks, stain orders (with the
    instrument running each one and that instrument's status), flags and recent history.
    Patient identifiers are never returned."""
    _maybe_fault()
    with _conn() as c:
        spec = _specimen(c, accession)
        now = datetime.now(timezone.utc)
        due = datetime.fromisoformat(spec["due_at"])
        spec["overdue"] = spec["status"] != "signed_out" and due < now
        spec["overdue_hours"] = round((now - due).total_seconds() / 3600) if spec["overdue"] else 0
        spec["block_labels"] = _block_labels(spec["blocks"])
        spec["stain_orders"] = [dict(r) for r in c.execute(
            "SELECT o.block, o.stain, o.status, o.instrument_id, i.status AS instrument_status, "
            "i.note AS instrument_note, o.ordered_at, o.ordered_by "
            "FROM stain_orders o LEFT JOIN instruments i ON i.id = o.instrument_id "
            "WHERE o.accession = ? ORDER BY o.id", (spec["accession"],))]
        spec["recent_events"] = [dict(r) for r in c.execute(
            "SELECT ts, event, actor FROM events WHERE accession = ? ORDER BY ts DESC, id DESC LIMIT 5",
            (spec["accession"],))]
    spec["headline"] = _headline(spec)
    return spec


def _headline(spec: dict[str, Any]) -> str:
    """A precomputed, speakable summary. Small models reliably repeat a good headline; they
    are much less reliable at assembling one from nested JSON."""
    parts = [f"{spec['accession']}: {spec['priority']} {spec['site']} {spec['type']}, "
             f"{spec['status'].replace('_', ' ')}"]
    if spec["overdue"]:
        parts.append(f"OVERDUE by {spec['overdue_hours']} hours")
    if spec["pathologist"]:
        parts.append(spec["pathologist"])
    for o in spec["stain_orders"]:
        if o["status"] != "complete":
            line = f"{o['stain']} on {o['block']} {o['status'].replace('_', ' ')}"
            if o["instrument_id"]:
                line += f" on {o['instrument_id']}"
                if o["instrument_status"] and o["instrument_status"] != "online":
                    line += (f", which is BLOCKED ({o['instrument_status'].replace('_', ' ')}"
                             + (f": {o['instrument_note']}" if o["instrument_note"] else "") + ")")
            parts.append(line)
    if spec["flag"]:
        parts.append(f"FLAGGED: {spec['flag']}")
    return "; ".join(parts) + "."


@mcp.tool(annotations=READ)
def find_specimens(
    status: Literal["received", "grossed", "processing", "embedded", "sectioned",
                    "stained", "slides_ready", "under_review", "signed_out"] | None = None,
    priority: Literal["stat", "routine"] | None = None,
    pathologist: str | None = None,
    overdue_only: bool = False,
    flagged_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Search specimens by workflow status, priority, assigned pathologist (name fragment OK),
    overdue (past turnaround-time due date and not signed out) or flagged. Sorted by due time."""
    _maybe_fault()
    sql, args = f"SELECT {SPECIMEN_COLS} FROM specimens WHERE 1=1", []
    if status:
        sql += " AND status = ?"; args.append(status)
    if priority:
        sql += " AND priority = ?"; args.append(priority)
    if pathologist:
        sql += " AND lower(pathologist) LIKE ?"; args.append(f"%{pathologist.lower().replace('dr.', '').strip()}%")
    if overdue_only:
        sql += " AND status != 'signed_out' AND due_at < ?"; args.append(_now())
    if flagged_only:
        sql += " AND flag IS NOT NULL"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql + " ORDER BY due_at", args)]
    limit = max(1, min(limit, 25))
    return {"total": len(rows), "returned": min(limit, len(rows)), "specimens": rows[:limit]}


@mcp.tool(annotations=READ)
def get_instrument(instrument: str) -> dict[str, Any]:
    """Get an instrument's status, note (e.g. repair ETA), last 3 QC runs, and the stains currently
    loaded on it. Pass what the user said: an ID (IHC-01) or a spoken name ("IHC stainer one",
    "the H&E stainer")."""
    _maybe_fault()
    with _conn() as c:
        inst = _instrument(c, instrument)
        inst["recent_qc"] = [dict(r) for r in c.execute(
            "SELECT run_at, result, notes, performed_by FROM qc_runs WHERE instrument_id = ? ORDER BY run_at DESC LIMIT 3",
            (inst["id"],))]
        inst["in_progress"] = [dict(r) for r in c.execute(
            "SELECT accession, block, stain FROM stain_orders WHERE instrument_id = ? AND status = 'in_progress'",
            (inst["id"],))]
    return inst


@mcp.tool(annotations=READ)
def list_instruments(
    available: bool | None = None,
    status: Literal["online", "maintenance", "qc_hold", "offline"] | None = None,
) -> dict[str, Any]:
    """List lab instruments and their status. An instrument is available only when its status is
    'online'; 'maintenance', 'qc_hold' and 'offline' all mean it is DOWN / unavailable.
    Use available=false to find everything that is down; available=true for what's usable."""
    _maybe_fault()
    with _conn() as c:
        sql, args, where = "SELECT id, name, type, status, note FROM instruments", [], []
        if available is not None:
            where.append("status = 'online'" if available else "status != 'online'")
        if status:
            where.append("status = ?"); args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = [{**dict(r), "available": r["status"] == "online"} for r in c.execute(sql + " ORDER BY id", args)]
    down = [r["id"] for r in rows if not r["available"]]
    return {"count": len(rows), "down": down, "instruments": rows}


# ----------------------------------------------------------------- writes ---
@mcp.tool(annotations=WRITE)
def order_stain(accession: str, stain: str, block: str = "A1", reason: str | None = None,
                dry_run: bool = False, actor: str | None = None) -> dict[str, Any]:
    """Order a stain (H&E, special stain, or IHC) on a block of a specimen. Writes to the LIMS;
    held for supervisor approval."""
    _maybe_fault()
    canonical = {s.lower(): s for s in STAINS}.get(stain.strip().lower())
    if not canonical:
        raise ToolError(f"Unknown stain '{stain}'. Orderable: {', '.join(STAINS)}")
    with _conn() as c:
        spec = _specimen(c, accession)
        labels = _block_labels(spec["blocks"])
        if not labels:
            raise ToolError(f"{spec['accession']} has no blocks yet (status: {spec['status']}).")
        block = block.strip().upper()
        if block not in labels:
            raise ToolError(f"{spec['accession']} has blocks {', '.join(labels)}; there is no {block}.")
        duplicate = c.execute("SELECT status FROM stain_orders WHERE accession = ? AND block = ? AND stain = ?",
                              (spec["accession"], block, canonical)).fetchone()
        preview = {"accession": spec["accession"], "block": block, "stain": canonical,
                   "specimen_status": spec["status"], "priority": spec["priority"],
                   "warning": f"{canonical} already {duplicate['status'].replace('_', ' ')} on {block}" if duplicate else None}
        if dry_run:
            return {"dry_run": True, "summary": f"order {canonical} on {spec['accession']} block {block}", **preview}
        cur = c.execute(
            "INSERT INTO stain_orders (accession, block, stain, status, instrument_id, ordered_at, ordered_by, reason) "
            "VALUES (?, ?, ?, 'ordered', NULL, ?, ?, ?)",
            (spec["accession"], block, canonical, _now(), _actor(actor), reason))
        _event(c, spec["accession"], f"stain ordered: {canonical} on {block}" + (f" ({reason})" if reason else ""),
               _actor(actor))
    return {"order_id": cur.lastrowid, "accession": spec["accession"], "block": block, "stain": canonical,
            "status": "ordered"}


@mcp.tool(annotations=WRITE)
def flag_specimen(accession: str, reason: str, dry_run: bool = False, actor: str | None = None) -> dict[str, Any]:
    """Flag a specimen for a problem (labeling discrepancy, insufficient tissue, etc.).
    Flagged specimens show on the supervisor's worklist. Held for approval."""
    _maybe_fault()
    if len(reason.strip()) < 5:
        raise ToolError("A flag needs a specific reason.")
    with _conn() as c:
        spec = _specimen(c, accession)
        if dry_run:
            return {"dry_run": True, "summary": f"flag {spec['accession']}: {reason.strip()}", "accession": spec["accession"],
                    "reason": reason.strip(), "replaces_flag": spec["flag"]}
        c.execute("UPDATE specimens SET flag = ? WHERE accession = ?", (reason.strip(), spec["accession"]))
        _event(c, spec["accession"], f"flagged: {reason.strip()}", _actor(actor))
    return {"accession": spec["accession"], "flag": reason.strip()}


@mcp.tool(annotations=DESTRUCTIVE)
def set_instrument_status(instrument: str, status: Literal["online", "maintenance", "qc_hold", "offline"],
                          note: str, dry_run: bool = False, actor: str | None = None) -> dict[str, Any]:
    """Change an instrument's status (e.g. put it on QC hold or bring it back online).
    Affects every specimen routed to it. Held for supervisor approval."""
    _maybe_fault()
    with _conn() as c:
        inst = _instrument(c, instrument)
        if status == "online":
            last = c.execute("SELECT result FROM qc_runs WHERE instrument_id = ? ORDER BY run_at DESC LIMIT 1",
                             (inst["id"],)).fetchone()
            if last and last["result"] == "fail":
                raise ToolError(f"{inst['id']}'s most recent QC failed; it cannot go online until QC passes.")
        affected = c.execute("SELECT COUNT(*) FROM stain_orders WHERE instrument_id = ? AND status = 'in_progress'",
                             (inst["id"],)).fetchone()[0]
        if dry_run:
            return {"dry_run": True, "summary": f"set {inst['id']} ({inst['name']}) from {inst['status']} to {status}"
                               + (f", affecting {affected} stain(s) in progress" if affected else ""),
                    "instrument": inst["id"],
                    "from": inst["status"], "to": status, "in_progress_stains_affected": affected}
        c.execute("UPDATE instruments SET status = ?, note = ? WHERE id = ?", (status, note, inst["id"]))
        c.execute("INSERT INTO instrument_log (instrument_id, ts, change, actor) VALUES (?, ?, ?, ?)",
                  (inst["id"], _now(), f"{inst['status']} -> {status}: {note}", _actor(actor)))
    return {"instrument": inst["id"], "previous_status": inst["status"], "status": status, "note": note}


if __name__ == "__main__":
    init_db()
    mcp.run("stdio")
