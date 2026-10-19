"""The lab at a glance, for the client's empty state. Read-only LIMS tools, called by the host."""
from __future__ import annotations

from typing import Any

from voiceops.mcp_host import MCPHost


async def lab_overview(host: MCPHost) -> dict[str, Any]:
    overdue = await host.call("lims__find_specimens", {"overdue_only": True, "limit": 1})
    flagged = await host.call("lims__find_specimens", {"flagged_only": True, "limit": 1})
    down = await host.call("lims__list_instruments", {"available": False})
    for outcome in (overdue, flagged, down):
        if not outcome.ok:
            raise RuntimeError(outcome.error)
    return {
        "overdue": overdue.data["total"],
        "flagged": flagged.data["total"],
        "down": [{"id": i["id"], "name": i["name"], "status": i["status"]} for i in down.data["instruments"]],
    }
