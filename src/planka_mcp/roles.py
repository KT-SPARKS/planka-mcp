"""Role and capability model.

Planka 2.4.1 has two role axes, and the swagger documents neither correctly:

* instance role - `admin`, `projectOwner`, `boardUser`, `guestOnly`
* board membership role - `editor`, `worker`, `guest`

This module folds both into one effective role per board, maps that role to
capabilities, and lets the operator cap the agent *below* its account's real
rights with `PLANKA_ACT_AS`. Guests are read-only here; they are not modelled
in detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Capabilities, smallest to largest.
READ = "read"          # see boards, tasks, people
WORK = "work"          # claim/release own work, move own tasks, report results
AUTHOR = "author"      # create and edit tasks, labels on tasks, checklists
ASSIGN = "assign"      # put other people on a task
STRUCTURE = "structure"  # lists, labels, board (tab) creation and renaming
PEOPLE = "people"      # board membership and member roles
ADMIN = "admin"        # instance roles, creating users, project managers

ROLE_ORDER = ("guest", "worker", "editor", "admin")

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "guest": frozenset({READ}),
    "worker": frozenset({READ, WORK}),
    "editor": frozenset({READ, WORK, AUTHOR, ASSIGN, STRUCTURE}),
    "admin": frozenset({READ, WORK, AUTHOR, ASSIGN, STRUCTURE, PEOPLE, ADMIN}),
}

# What each capability means when the agent is told it lacks one.
CAPABILITY_HINT = {
    WORK: "claim tasks or change their status",
    AUTHOR: "create or edit tasks",
    ASSIGN: "assign other people to a task",
    STRUCTURE: "change lists, labels or board structure",
    PEOPLE: "change who is a member of this board",
    ADMIN: "administer users or instance roles",
}

BOARD_ROLES = ("editor", "worker", "guest")
INSTANCE_ROLES = ("admin", "projectOwner", "boardUser", "guestOnly")


@dataclass(frozen=True)
class Effective:
    """The role actually in force for one board, and why."""

    role: str
    source: str
    account_role: str
    capped_to: str | None = None

    @property
    def capabilities(self) -> frozenset[str]:
        return ROLE_CAPABILITIES.get(self.role, frozenset({READ}))

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "role": self.role,
            "granted_by": self.source,
            "capabilities": sorted(self.capabilities),
        }
        if self.capped_to:
            info["capped_by_config"] = self.capped_to
            info["account_role_without_cap"] = self.account_role
        return info


def cap_role(role: str, ceiling: str | None) -> tuple[str, str | None]:
    """Lower `role` to `ceiling` when the operator configured PLANKA_ACT_AS."""
    if not ceiling or ceiling not in ROLE_ORDER:
        return role, None
    if ROLE_ORDER.index(ceiling) < ROLE_ORDER.index(role):
        return ceiling, ceiling
    return role, None


def resolve_effective_role(
    *,
    user_id: str,
    instance_role: str | None,
    board_memberships: list[dict[str, Any]],
    project_manager_user_ids: set[str],
    board_id: str,
    act_as: str | None,
) -> Effective:
    """Fold instance role + project managership + board membership into one role."""
    account = "guest"
    source = "no membership on this board"

    membership = next(
        (
            m
            for m in board_memberships
            if str(m.get("userId")) == user_id and str(m.get("boardId")) == board_id
        ),
        None,
    )
    if membership is not None:
        board_role = str(membership.get("role") or "guest")
        account = board_role if board_role in ROLE_ORDER else "guest"
        source = f"board membership ({board_role})"

    if user_id in project_manager_user_ids:
        account = "admin"
        source = "project manager of the parent board"

    if instance_role == "admin":
        account = "admin"
        source = "instance admin"

    role, capped = cap_role(account, act_as)
    return Effective(role=role, source=source, account_role=account, capped_to=capped)


def denial(effective: Effective, capability: str, what: str) -> dict[str, Any]:
    """A refusal the model can act on: what was denied, and why."""
    result = {
        "ok": False,
        "result": "not_permitted",
        "reason": f"Your role here is '{effective.role}' ({effective.source}), "
                  f"which cannot {CAPABILITY_HINT.get(capability, capability)}.",
        "attempted": what,
        "your_capabilities": sorted(effective.capabilities),
    }
    if effective.capped_to:
        result["note"] = (
            f"The account itself is '{effective.account_role}', but this server is "
            f"configured to act as '{effective.capped_to}' (PLANKA_ACT_AS)."
        )
    return result
