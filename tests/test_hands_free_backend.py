"""Backend pieces of hands-free mode: the lab snapshot and the phantom-transcript filter."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from voiceops.config import default_servers
from voiceops.mcp_host import MCPHost
from voiceops.overview import lab_overview
from voiceops.stt import DOMAIN_PROMPT, clean_transcript


def seg(text, no_speech_prob=0.01, avg_logprob=-0.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


def test_lab_overview_reflects_the_lims():
    async def main():
        with tempfile.TemporaryDirectory() as d:
            async with MCPHost(default_servers(str(Path(d) / "lims.db"))) as host:
                o = await lab_overview(host)
        assert o["overdue"] >= 1          # S26-00287 is planted overdue
        assert o["flagged"] >= 1          # S26-00330 is planted flagged
        assert {i["id"] for i in o["down"]} == {"HE-01", "IHC-01"}
    anyio.run(main)


@pytest.mark.parametrize("segments,expected", [
    ([seg(" Why isn't 312 ready yet?")], "Why isn't 312 ready yet?"),
    ([seg(" Thank you.")], ""),                                     # classic Whisper phantom
    ([seg(" you")], ""),
    ([seg(" Is the stainer up?"), seg(" Thanks for watching!", 0.9, -1.4)], "Is the stainer up?"),
    ([seg(" mumble", 0.8, -1.5)], ""),                               # scored as not speech
    ([seg(" " + DOMAIN_PROMPT[:60])], ""),                           # echo of the vocabulary prompt
    ([seg(" Thank you, order a PAS on 312.")], "Thank you, order a PAS on 312."),  # real speech kept
    ([], ""),
])
def test_clean_transcript(segments, expected):
    assert clean_transcript(segments) == expected
