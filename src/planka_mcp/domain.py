"""Planka -> agent-workflow translation layer.

Planka has no status, priority or effort field. Status is *which list a card
sits in*; priority and effort only exist as label conventions. This module turns
the raw board graph into the small, decision-shaped view the tools expose.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

TODO = "todo"
IN_PROGRESS = "in_progress"
REVIEW = "review"
DONE = "done"

STATUSES = (TODO, IN_PROGRESS, REVIEW, DONE)

# Legal transitions. Enforced server-side; the model cannot jump todo -> done.
TRANSITIONS: dict[str, set[str]] = {
    TODO: {IN_PROGRESS},
    IN_PROGRESS: {REVIEW, DONE, TODO},
    REVIEW: {DONE, IN_PROGRESS},
    DONE: set(),
}
REOPEN_TRANSITIONS: set[str] = {TODO, IN_PROGRESS}

# List-name heuristics, checked longest-phrase-first.
NAME_HINTS: dict[str, tuple[str, ...]] = {
    IN_PROGRESS: (
        "in progress", "inprogress", "in-progress", "work in progress", "wip",
        "doing", "started", "active", "current", "ongoing", "in dev", "development",
    ),
    REVIEW: (
        "in review", "code review", "review", "qa", "testing", "test", "verify",
        "validation", "approval", "staging",
    ),
    DONE: (
        "done", "completed", "complete", "finished", "shipped", "released",
        "closed", "archive done",
    ),
    TODO: (
        "to do", "todo", "to-do", "backlog", "ready", "up next", "next", "open",
        "new", "inbox", "queue", "planned", "sprint backlog",
    ),
}

PRIORITY_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(p0|urgent|critical|blocker|highest|now)\b"), 0),
    (re.compile(r"\b(p1|high|important)\b"), 1),
    (re.compile(r"\b(p2|medium|normal|moderate)\b"), 2),
    (re.compile(r"\b(p3|p4|low|minor|nice to have|someday|lowest)\b"), 3),
)
PRIORITY_NAMES = {0: "urgent", 1: "high", 2: "medium", 3: "low"}
DEFAULT_PRIORITY = 2

EFFORT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(xs|extra small)\b"), "xs"),
    (re.compile(r"\bs(ize)?[: ]*s\b|\bsmall\b"), "s"),
    (re.compile(r"\bs(ize)?[: ]*m\b|\bmedium\b"), "m"),
    (re.compile(r"\bs(ize)?[: ]*l\b|\blarge\b"), "l"),
    (re.compile(r"\b(xl|xxl|extra large|epic)\b"), "xl"),
)
POINTS_RE = re.compile(r"\b(?:sp|points?|estimate)[: ]*(\d+(?:\.\d+)?)\b")


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


# Types a list can be created with.
LIST_TYPES = ("active", "closed", "waiting", "inactive")
# Types that take part in the todo -> done flow.
FLOW_TYPES = ("active", "closed")
# Types Planka creates on its own, per board; never work stages.
SYSTEM_TYPES = ("inbox", "recurring", "archive", "trash")


def classify_list(
    list_obj: dict[str, Any], overrides: dict[str, Any], board_id: str
) -> str | None:
    """Map a Planka list to a canonical status, or None if it is not a work stage."""
    list_id = str(list_obj.get("id") or "")
    list_type = list_obj.get("type") or "active"

    board_override = overrides.get(board_id)
    if isinstance(board_override, dict):
        for status, value in board_override.items():
            if status in STATUSES and str(value) == list_id:
                return status

    name = _norm(list_obj.get("name"))
    for status, names in overrides.items():
        if status in STATUSES and isinstance(names, list):
            if any(_norm(str(n)) == name for n in names):
                return status

    # Only two list types carry the work flow. Everything else - waiting,
    # inactive, and the system lists Planka creates per board (inbox, recurring,
    # archive, trash) - is deliberately outside it. Allowlist, not blocklist, so
    # a named system list can never be mistaken for a work stage.
    if list_type not in FLOW_TYPES:
        return None
    if list_type == "closed":
        return DONE
    if not name:
        return None
    for status in (IN_PROGRESS, REVIEW, DONE, TODO):
        for hint in NAME_HINTS[status]:
            if name == hint or hint in name:
                return status
    return None


def derive_priority(label_names: Iterable[str]) -> tuple[int, str | None]:
    """Priority rank (0 = most urgent) plus the label text it came from."""
    best: tuple[int, str | None] = (DEFAULT_PRIORITY, None)
    found = False
    for raw in label_names:
        name = _norm(raw)
        for pattern, rank in PRIORITY_PATTERNS:
            if pattern.search(name):
                if not found or rank < best[0]:
                    best = (rank, raw)
                found = True
                break
    return best


def derive_effort(label_names: Iterable[str], task_count: int) -> str | None:
    for raw in label_names:
        name = _norm(raw)
        # "priority: medium" is a priority, not a size - never read it as one.
        if any(pattern.search(name) for pattern, _ in PRIORITY_PATTERNS):
            continue
        points = POINTS_RE.search(name)
        if points:
            return f"{points.group(1)} points"
        for pattern, size in EFFORT_PATTERNS:
            if pattern.search(name):
                return size
    if task_count:
        return f"{task_count} subtasks"
    return None


def parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_until(due: datetime | None, now: datetime | None = None) -> int | None:
    if due is None:
        return None
    now = now or datetime.now(timezone.utc)
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return round((due - now).total_seconds() / 86400, 1)


class BoardView:
    """Indexed view over one `GET /boards/{id}` payload."""

    def __init__(self, payload: dict[str, Any], overrides: dict[str, Any]):
        self.board = payload.get("item") or {}
        included = payload.get("included") or {}
        self.board_id = str(self.board.get("id") or "")
        self.board_name = self.board.get("name")
        self.project_id = str(self.board.get("projectId") or "")
        self.board_memberships = list(included.get("boardMemberships") or [])

        self.lists = {str(l["id"]): l for l in included.get("lists") or [] if l.get("id")}
        self.cards = {str(c["id"]): c for c in included.get("cards") or [] if c.get("id")}
        self.labels = {str(l["id"]): l for l in included.get("labels") or [] if l.get("id")}
        self.users = {str(u["id"]): u for u in included.get("users") or [] if u.get("id")}

        self.status_of_list = {
            list_id: classify_list(obj, overrides, self.board_id)
            for list_id, obj in self.lists.items()
        }
        self.list_of_status: dict[str, str] = {}
        for list_id, status in self.status_of_list.items():
            if status and status not in self.list_of_status:
                self.list_of_status[status] = list_id
            elif status:
                current = self.lists[self.list_of_status[status]].get("position") or 0
                if (self.lists[list_id].get("position") or 0) < current:
                    self.list_of_status[status] = list_id

        self.members_of_card: dict[str, list[str]] = {}
        for membership in included.get("cardMemberships") or []:
            card_id = str(membership.get("cardId"))
            self.members_of_card.setdefault(card_id, []).append(str(membership.get("userId")))

        self.labels_of_card: dict[str, list[str]] = {}
        self.label_ids_of_card: dict[str, list[str]] = {}
        self.cards_of_label: dict[str, list[str]] = {}
        for card_label in included.get("cardLabels") or []:
            card_id = str(card_label.get("cardId"))
            label_id = str(card_label.get("labelId"))
            self.cards_of_label.setdefault(label_id, []).append(card_id)
            label = self.labels.get(label_id)
            if label:
                self.labels_of_card.setdefault(card_id, []).append(label.get("name") or "")
                self.label_ids_of_card.setdefault(card_id, []).append(label_id)

        self.task_lists = {
            str(t["id"]): t for t in included.get("taskLists") or [] if t.get("id")
        }
        self.tasks_of_card: dict[str, list[dict[str, Any]]] = {}
        for task in included.get("tasks") or []:
            parent = self.task_lists.get(str(task.get("taskListId")))
            if not parent:
                continue
            self.tasks_of_card.setdefault(str(parent.get("cardId")), []).append(task)

    # ---- per-card derivations ----

    def status_of_card(self, card: dict[str, Any]) -> str | None:
        if card.get("isClosed"):
            return DONE
        return self.status_of_list.get(str(card.get("listId")))

    def list_name(self, card: dict[str, Any]) -> str | None:
        obj = self.lists.get(str(card.get("listId"))) or {}
        return obj.get("name")

    def blocking_deps(self, card_id: str) -> list[dict[str, Any]]:
        """Unfinished cards linked from this card's checklist tasks."""
        blocking = []
        for task in self.tasks_of_card.get(card_id, []):
            linked_id = task.get("linkedCardId")
            if not linked_id or task.get("isCompleted"):
                continue
            linked = self.cards.get(str(linked_id))
            if linked is None:
                blocking.append({"card_id": str(linked_id), "title": task.get("name"),
                                 "status": "unknown (other board)"})
                continue
            status = self.status_of_card(linked)
            if status != DONE:
                blocking.append({"card_id": str(linked_id), "title": linked.get("name"),
                                 "status": status or "unmapped"})
        return blocking

    def summarize(self, card: dict[str, Any], my_id: str) -> dict[str, Any]:
        card_id = str(card.get("id"))
        label_names = self.labels_of_card.get(card_id, [])
        # Checklist entries that only link another card are dependencies, not work.
        tasks = [
            t for t in self.tasks_of_card.get(card_id, []) if not t.get("linkedCardId")
        ]
        members = self.members_of_card.get(card_id, [])
        rank, priority_label = derive_priority(label_names)
        due = parse_due(card.get("dueDate"))
        description = (card.get("description") or "").strip()
        return {
            "task_id": card_id,
            "title": card.get("name"),
            "status": self.status_of_card(card) or "unmapped",
            "board": self.board_name,
            "board_id": self.board_id,
            "list": self.list_name(card),
            "priority": PRIORITY_NAMES.get(rank, "medium"),
            "priority_label": priority_label,
            "due_date": card.get("dueDate"),
            "days_until_due": days_until(due),
            "effort": derive_effort(label_names, len(tasks)),
            "labels": label_names,
            "subtasks": {
                "total": len(tasks),
                "done": sum(1 for t in tasks if t.get("isCompleted")),
            },
            "comments": card.get("commentsTotal", 0),
            "claimed_by_me": my_id in members,
            "assignees": [
                (self.users.get(m) or {}).get("name") or m for m in members
            ],
            "description": description[:400] + ("..." if len(description) > 400 else ""),
            "_rank": rank,
            "_due": due,
            "_position": card.get("position") or 0,
        }

    # ---- structure helpers used by the management tools ----

    def cards_in_list(self, list_id: str) -> list[dict[str, Any]]:
        return [c for c in self.cards.values() if str(c.get("listId")) == list_id]

    def find_list(self, name_or_id: str) -> dict[str, Any] | None:
        if name_or_id in self.lists:
            return self.lists[name_or_id]
        wanted = _norm(name_or_id)
        for obj in self.lists.values():
            if _norm(obj.get("name")) == wanted:
                return obj
        return None

    def find_label(self, name_or_id: str) -> dict[str, Any] | None:
        if name_or_id in self.labels:
            return self.labels[name_or_id]
        wanted = _norm(name_or_id)
        for obj in self.labels.values():
            if _norm(obj.get("name")) == wanted:
                return obj
        return None

    def membership_of(self, user_id: str) -> dict[str, Any] | None:
        return next(
            (m for m in self.board_memberships if str(m.get("userId")) == user_id), None
        )

    def structure(self) -> dict[str, Any]:
        """Board layout as the management tools report it."""
        lists = sorted(self.lists.values(), key=lambda l: l.get("position") or 0)
        return {
            "project_id": self.board_id,
            "name": self.board_name,
            "board_id": self.project_id,
            "lists": [
                {
                    "list_id": str(l["id"]),
                    "name": l.get("name"),
                    "type": l.get("type") or "active",
                    "maps_to_status": self.status_of_list.get(str(l["id"])) or "not a work stage",
                    "cards": len(self.cards_in_list(str(l["id"]))),
                }
                for l in lists
            ],
            "labels": [
                {
                    "label_id": str(l["id"]),
                    "name": l.get("name"),
                    "color": l.get("color"),
                    "used_on_cards": len(self.cards_of_label.get(str(l["id"]), [])),
                }
                for l in sorted(self.labels.values(), key=lambda l: l.get("position") or 0)
            ],
            "members": [
                {
                    "user_id": str(m.get("userId")),
                    "name": (self.users.get(str(m.get("userId"))) or {}).get("name"),
                    "email": (self.users.get(str(m.get("userId"))) or {}).get("email"),
                    "role": m.get("role"),
                    "membership_id": str(m.get("id")),
                }
                for m in self.board_memberships
            ],
            "cards_total": len(self.cards),
        }


def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Most urgent first: priority, then soonest deadline, then board position."""
    due = item.get("_due")
    return (
        item["_rank"],
        0 if due else 1,
        due.timestamp() if due else 0,
        item["_position"],
    )


def next_position(positions: Iterable[float]) -> float:
    values = [p for p in positions if isinstance(p, (int, float))]
    return (max(values) + 65536) if values else 65536
