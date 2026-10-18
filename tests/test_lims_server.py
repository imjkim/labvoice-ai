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
