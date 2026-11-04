"""The lab's standard operating procedures (SOPs) as an MCP server (MCP Python SDK 2.x).

Run standalone:  python -m servers.sop_server
The agent host launches it next to the LIMS server, so one question can combine LIMS state
("IHC-01 is on QC hold") with procedure ("what to do when an IHC stainer fails QC").

Both tools are read-only. Results carry a citation ("SOP-420 §3") and how to say it aloud
("SOP 420, section 3"); the agent's grounding guard rejects answers that cite a section no search
in the same turn returned. When nothing matches well, the result says so explicitly, so the model
is told to admit it rather than answer from general knowledge.
"""
from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from servers.sop_index import SopIndex

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)

mcp = MCPServer("sops", instructions="The histology lab's standard operating procedures, searchable by section.")
_index: SopIndex | None = None


def index() -> SopIndex:
    global _index
    if _index is None:
        _index = SopIndex.from_env()
    return _index


@mcp.tool(annotations=READ)
def search_sops(
    question: Annotated[str, Field(description=(
        "The procedure question in plain words, e.g. 'what to do when a stainer fails QC' "
        "(example only). Include the stain, instrument type or step the user mentioned."))],
    k: Annotated[int, Field(description="How many sections to return (1-5).")] = 3,
) -> dict[str, Any]:
    """Search the lab's SOPs for how to do something or what the rules are: QC failures, instrument
    downtime, stain controls, fixation times, sectioning, labeling problems, turnaround and
    escalation, who may approve what. Returns the best-matching sections with their text and a
    citation. Answer only from the returned text and name its cite_as (like 'SOP 999, section 2', example only). If
    coverage is 'weak', the SOPs don't cover the question."""
    if not question.strip():
        raise ToolError("Ask a question to search the SOPs.")
    return index().search(question, k=max(1, min(k, 5)))


@mcp.tool(annotations=READ)
def read_sop(
    ref: Annotated[str, Field(description=(
        "A citation from a search result, like 'SOP-999 §2' (example only), or an SOP number alone "
        "for its list of sections."))],
) -> dict[str, Any]:
    """Read one SOP section in full by citation, or list an SOP's sections. Use after search_sops
    when the user asks about a specific SOP or section."""
    try:
        return index().read(ref)
    except KeyError:
        known = ", ".join(sorted({c.sop for c in index().all_chunks}))
        raise ToolError(f"No SOP section '{ref}'. SOPs: {known}. Search with search_sops instead.") from None


if __name__ == "__main__":
    index()  # parse the corpus up front so a broken SOP file fails at startup, not mid-question
    mcp.run("stdio")
