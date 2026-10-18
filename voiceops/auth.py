"""Who is acting, and who may approve what.

Identity here is a *demo directory* selected by an X-User header. A real
deployment would take identity from the lab's SSO (OIDC/SAML) and roles from
its directory; the approval policy below is the part that carries over.
"""
from __future__ import annotations

from dataclasses import dataclass

ROLES = ("tech", "lead_tech", "supervisor", "pathologist")


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str

    def public(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "role": self.role}


DEMO_USERS: dict[str, User] = {u.id: u for u in [
    User("mchen", "M. Chen", "tech"),
    User("jalvarez", "J. Alvarez", "tech"),
    User("rpatel", "R. Patel", "lead_tech"),
    User("dreyes", "D. Reyes", "supervisor"),
    User("cruiz", "Dr. Carmen Ruiz", "pathologist"),
]}

# Which roles may approve each write tool. Pathologists own stain orders, lead techs and
# supervisors handle specimen problems, and only supervisors change instrument status.
APPROVAL_POLICY: dict[str, frozenset[str]] = {
    "lims__order_stain": frozenset({"pathologist", "supervisor"}),
    "lims__flag_specimen": frozenset({"lead_tech", "supervisor"}),
    "lims__set_instrument_status": frozenset({"supervisor"}),
}
# Any write tool not listed (a new tool, a third-party server) needs the most senior role.
DEFAULT_APPROVERS: frozenset[str] = frozenset({"supervisor"})


def approver_roles(tool: str) -> frozenset[str]:
    return APPROVAL_POLICY.get(tool, DEFAULT_APPROVERS)


def check_decision(tool: str, requested_by: str | None, approver: User, approve: bool) -> str | None:
    """Return None if `approver` may make this decision, else the reason they may not.

    - Approving requires an allowed role AND a different person from the requester (four eyes).
    - Denying requires an allowed role, except that requesters may withdraw their own request.
    """
    if not approve and requested_by == approver.id:
        return None
    if approve and requested_by == approver.id:
        return "you can't approve your own request"
    allowed = approver_roles(tool)
    if approver.role not in allowed:
        return f"{tool} requires approval by: {', '.join(sorted(allowed))} (you are {approver.role})"
    return None
