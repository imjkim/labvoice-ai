"""Tests for the LIMS MCP server itself: ID tolerance, validation, and the
data-minimization guarantee (patient identifiers never leave the server)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import tempfile
from pathlib import Path

import anyio
import pytest

from servers.lims_server import normalize_accession
from voiceops.config import default_servers
from voiceops.mcp_host import MCPHost


@pytest.mark.parametrize("spoken,canonical", [
    ("S26-00312", "S26-00312"), ("s 26 312", "S26-00312"), ("S26312", "S26-00312"),
    ("26-312", "S26-00312"), ("S26 - 0312", "S26-00312"), ("2600312", "S26-00312"),
    ("312", "S26-00312"), ("00312", "S26-00312"), ("S25-00999", "S25-00999"),
])
def test_accession_normalization(spoken, canonical):
    assert normalize_accession(spoken, year=2026) == canonical


@pytest.mark.parametrize("bad", ["the breast one", "IHC-01", "", "1-2-3", "S26-1234567"])
def test_non_accession_is_rejected(bad):
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError):
        normalize_accession(bad, year=2026)


def test_patient_identifiers_never_leave_the_server():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "lims.db")
            async with MCPHost(default_servers(db)) as host:
                listing = await host.call("lims__find_specimens", {"limit": 25})
                outputs = [json.dumps(listing.data)]
                for s in listing.data["specimens"]:
                    outputs.append(json.dumps((await host.call("lims__get_specimen", {"accession": s["accession"]})).data))
                outputs.append(json.dumps((await host.call("lims__list_instruments", {})).data))
            with closing(sqlite3.connect(db)) as c:
                patients = c.execute("SELECT id, name, dob, mrn FROM patients").fetchall()
            blob = "\n".join(outputs)
            assert patients
            for pid, name, dob, mrn in patients:
                for secret in (pid, name, dob, mrn):
                    assert secret not in blob, f"leaked patient identifier: {secret}"
    anyio.run(main)


def test_validation_errors_are_actionable():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                bad_block = await host.call("lims__order_stain", {"accession": "S26-00312", "stain": "HER2", "block": "A9"})
                assert "has blocks A1, A2" in bad_block.error
                no_blocks = await host.call("lims__order_stain", {"accession": "S26-00330", "stain": "H&E"})
                assert "no blocks yet" in no_blocks.error
                bad_stain = await host.call("lims__order_stain", {"accession": "S26-00312", "stain": "glitter"})
                assert "Orderable:" in bad_stain.error
                ambiguous = await host.call("lims__get_instrument", {"instrument": "IHC stainer"})
                assert "IHC-01" in ambiguous.error and "IHC-02" in ambiguous.error
    anyio.run(main)


def test_spoken_instrument_names_resolve():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                for spoken, expected in [("IHC stainer one", "IHC-01"), ("ihc 1", "IHC-01"), ("IHC-02", "IHC-02"),
                                         ("the H&E stainer", "HE-01"), ("tissue processor two", "TP-02")]:
                    out = await host.call("lims__get_instrument", {"instrument": spoken})
                    assert out.ok and out.data["id"] == expected, (spoken, out.error)
    anyio.run(main)


def test_headline_surfaces_blockers_overdue_and_flags():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                star = (await host.call("lims__get_specimen", {"accession": "312"})).data
                assert star["headline"].startswith("S26-00312:")
                assert "HER2" in star["headline"] and "IHC-01" in star["headline"] and "BLOCKED" in star["headline"]
                her2 = next(o for o in star["stain_orders"] if o["stain"] == "HER2")
                assert her2["instrument_status"] == "qc_hold"
                late = (await host.call("lims__get_specimen", {"accession": "287"})).data
                assert late["overdue"] and "OVERDUE" in late["headline"]
                flagged = (await host.call("lims__get_specimen", {"accession": "330"})).data
                assert "FLAGGED" in flagged["headline"]
    anyio.run(main)


def test_down_means_any_status_other_than_online():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                down = (await host.call("lims__list_instruments", {"available": False})).data
                assert set(down["down"]) == {"HE-01", "IHC-01"}  # maintenance + qc_hold, neither is 'offline'
                everything = (await host.call("lims__list_instruments", {})).data
                assert set(everything["down"]) == {"HE-01", "IHC-01"} and everything["count"] == 9
    anyio.run(main)
