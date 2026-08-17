"""In-memory Planka stand-in shared by the test modules.

It reproduces the semantics that matter: assignment is unique on (card, user) and
re-inserting answers 409; a card in a `closed` list is finished; board membership
carries a role; the instance role is a separate axis.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planka_mcp import app as A  # noqa: E402
from planka_mcp.config import Config  # noqa: E402

ME = "user-agent"
OTHER = "user-human"
ADA = "user-ada"
GRACE = "user-grace"
BOARD = "board-1"          # a "project" in the user's vocabulary
CONTAINER = "project-1"    # a "board" in the user's vocabulary
LIST_TODO, LIST_DOING, LIST_DONE = "list-todo", "list-doing", "list-done"
LIST_BLOCKED, LIST_EMPTY = "list-blocked", "list-empty"


class FakePlanka:
    """Minimal stand-in for PlankaClient with Planka's real semantics:
    card membership is unique on (cardId, userId) and re-inserting raises 409."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {
            "c1": {"id": "c1", "boardId": BOARD, "listId": LIST_TODO, "name": "Ready task",
                   "description": "do the thing", "position": 1, "isClosed": False,
                   "dueDate": None, "commentsTotal": 0},
            "c2": {"id": "c2", "boardId": BOARD, "listId": LIST_TODO, "name": "Someone else's",
                   "description": "", "position": 2, "isClosed": False, "dueDate": None,
                   "commentsTotal": 0},
            "c3": {"id": "c3", "boardId": BOARD, "listId": LIST_DONE, "name": "Shipped",
                   "description": "", "position": 1, "isClosed": False, "dueDate": None,
                   "commentsTotal": 0},
        }
        self.memberships: list[dict[str, Any]] = [
            {"cardId": "c2", "userId": OTHER, "createdAt": "2026-01-01T00:00:00.000Z"}
        ]
        self.comment_store: dict[str, list[dict[str, Any]]] = {}
        self.labels: list[dict[str, Any]] = [
            {"id": "lb1", "name": "priority: high"}, {"id": "lb2", "name": "size: M"},
        ]
        self.card_labels: list[dict[str, Any]] = []
        self.task_lists: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.races: dict[str, str] = {}  # card_id -> user who sneaks in mid-claim
        self.instance_role = "boardUser"
        self.board_memberships: list[dict[str, Any]] = [
            {"id": "bm1", "boardId": BOARD, "userId": ME, "role": "editor"},
            {"id": "bm2", "boardId": BOARD, "userId": OTHER, "role": "worker"},
        ]
        self.project_managers: list[dict[str, Any]] = []
        self.directory: list[dict[str, Any]] = [
            {"id": ME, "name": "Agent", "email": "agent@example.test", "role": "boardUser"},
            {"id": OTHER, "name": "Human", "email": "human@example.test", "role": "boardUser"},
            {"id": ADA, "name": "Ada Hopper", "email": "ada@example.test",
             "role": "boardUser"},
            {"id": GRACE, "name": "Grace Hopper", "email": "grace@example.test",
             "role": "boardUser"},
        ]
        self.deleted: list[str] = []
        self.minted = 0
        self.extra_lists: list[dict[str, Any]] = []
        self.hidden_lists: set[str] = set()
        self.calls: list[str] = []

    # --- reads ---
    async def my_id(self) -> str:
        return ME

    async def me(self) -> dict[str, Any]:
        return {"id": ME, "name": "Agent", "email": "agent@example.test",
                "role": self.instance_role}

    async def users(self) -> list[dict[str, Any]]:
        if self.instance_role != "admin":
            from planka_mcp.client import PlankaError
            raise PlankaError("Not enough rights", status=403)
        return list(self.directory)

    async def project_graph(self) -> dict[str, Any]:
        return {
            "items": [{"id": CONTAINER, "name": "Test container"}],
            "included": {
                "boards": [{"id": BOARD, "projectId": CONTAINER, "name": "Test board",
                            "type": "project"}],
                "projectManagers": list(self.project_managers),
                "boardMemberships": list(self.board_memberships),
            },
        }

    async def board_ids(self) -> list[str]:
        return [BOARD]

    def all_lists(self) -> list[dict[str, Any]]:
        base = [
            {"id": LIST_TODO, "name": "To Do", "type": "active", "position": 1},
            {"id": LIST_DOING, "name": "In Progress", "type": "active", "position": 2},
            {"id": LIST_DONE, "name": "Done", "type": "active", "position": 3},
            {"id": LIST_BLOCKED, "name": "Blocked", "type": "waiting", "position": 4},
            {"id": LIST_EMPTY, "name": "Icebox", "type": "inactive", "position": 5},
        ]
        if not hasattr(self, "_lists"):
            self._lists = base
        return [l for l in self._lists + self.extra_lists
                if l["id"] not in self.hidden_lists]

    async def board(self, board_id: str, *, fresh: bool = False) -> dict[str, Any]:
        return {
            "item": {"id": BOARD, "name": "Test board", "projectId": CONTAINER},
            "included": {
                "lists": self.all_lists(),
                "cards": list(self.cards.values()),
                "cardMemberships": list(self.memberships),
                "cardLabels": list(self.card_labels),
                "labels": list(self.labels),
                "users": [dict(u) for u in self.directory],
                "boardMemberships": list(self.board_memberships),
                "taskLists": list(self.task_lists),
                "tasks": list(self.tasks),
            },
        }

    async def card(self, card_id: str) -> dict[str, Any]:
        card = self.cards.get(card_id)
        if not card:
            return {}
        # A rival claim landing between the caller's read and its write.
        sneaky = self.races.pop(card_id, None)
        if sneaky:
            self.memberships.append(
                {"cardId": card_id, "userId": sneaky, "createdAt": "2020-01-01T00:00:00.000Z"}
            )
        members = [m for m in self.memberships if m["cardId"] == card_id]
        return {"item": card, "included": {"cardMemberships": members}}

    async def comments(self, card_id: str) -> list[dict[str, Any]]:
        return self.comment_store.get(card_id, [])

    # --- writes ---
    async def add_membership(self, card_id: str, user_id: str) -> tuple[int, dict[str, Any]]:
        self.calls.append(f"add_membership:{card_id}:{user_id}")
        if any(m["cardId"] == card_id and m["userId"] == user_id for m in self.memberships):
            return 409, {"code": "E_CONFLICT"}
        self.memberships.append(
            {"cardId": card_id, "userId": user_id, "createdAt": "2026-06-01T00:00:00.000Z"}
        )
        return 200, {"item": {"cardId": card_id, "userId": user_id}}

    async def remove_membership(self, card_id: str, user_id: str) -> None:
        self.calls.append(f"remove_membership:{card_id}:{user_id}")
        self.memberships = [
            m for m in self.memberships
            if not (m["cardId"] == card_id and m["userId"] == user_id)
        ]

    async def move_card(self, card_id: str, list_id: str, position: float) -> dict[str, Any]:
        self.calls.append(f"move:{card_id}:{list_id}")
        self.cards[card_id]["listId"] = list_id
        self.cards[card_id]["position"] = position
        return self.cards[card_id]

    async def create_comment(self, card_id: str, text: str) -> dict[str, Any]:
        self.calls.append(f"comment:{card_id}")
        entry = {"id": f"cm{len(self.comment_store.get(card_id, []))}", "text": text, "userId": ME}
        self.comment_store.setdefault(card_id, []).append(entry)
        return entry

    async def create_card(self, list_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        cid = f"c{len(self.cards) + 1}"
        card = {"id": cid, "boardId": BOARD, "listId": list_id, "isClosed": False,
                "commentsTotal": 0, **fields}
        self.cards[cid] = card
        self.calls.append(f"create_card:{cid}")
        return card

    async def patch_card(self, card_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.cards[card_id].update(fields)
        self.calls.append(f"patch_card:{card_id}:{','.join(sorted(fields))}")
        return self.cards[card_id]

    async def add_label(self, card_id: str, label_id: str) -> None:
        self.card_labels.append({"cardId": card_id, "labelId": label_id})

    async def create_task_list(self, card_id: str, name: str, position: float) -> dict[str, Any]:
        tl = {"id": f"tl{len(self.task_lists) + 1}", "cardId": card_id, "name": name}
        self.task_lists.append(tl)
        return tl

    async def create_task(self, task_list_id: str, name: str | None, position: float,
                          is_completed: bool = False, assignee_user_id: str | None = None,
                          linked_card_id: str | None = None) -> dict[str, Any]:
        t = {"id": f"t{len(self.tasks) + 1}", "taskListId": task_list_id, "name": name,
             "position": position, "isCompleted": is_completed,
             "assigneeUserId": assignee_user_id, "linkedCardId": linked_card_id}
        self.tasks.append(t)
        return t

    async def update_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        for t in self.tasks:
            if t["id"] == task_id:
                t.update(fields)
                self.calls.append(f"update_task:{task_id}:{','.join(sorted(fields))}")
                return t
        return {}

    async def set_task_completed(self, task_id: str, completed: bool) -> dict[str, Any]:
        for t in self.tasks:
            if t["id"] == task_id:
                t["isCompleted"] = completed
                self.calls.append(f"toggle:{task_id}:{completed}")
                return t
        return {}

    async def attach_link(self, card_id: str, url: str, name: str) -> dict[str, Any]:
        self.calls.append(f"attach:{card_id}:{url}")
        self.attachments.append({"id": f"a{len(self.attachments) + 1}", "cardId": card_id,
                                 "url": url, "name": name})
        return self.attachments[-1]

    def invalidate_board(self, board_id: str | None) -> None:
        pass

    # ---- structure ----
    async def create_list(self, board_id, name, position, list_type="active", color=None):
        # stands in for PlankaClient, which applies colour via a follow-up PATCH
        obj = {"id": f"list-{len(self.extra_lists) + 1}", "boardId": board_id,
               "name": name, "position": position, "type": list_type, "color": color}
        self.extra_lists.append(obj)
        return obj

    async def update_list(self, list_id, fields):
        for obj in self.all_lists():
            if obj["id"] == list_id:
                obj.update(fields)
                return obj
        # a list created moments ago lives in extra_lists
        for obj in self.extra_lists:
            if obj["id"] == list_id:
                obj.update(fields)
                return obj
        return {}

    async def delete_list(self, list_id):
        self.deleted.append(f"list:{list_id}")
        self.extra_lists = [l for l in self.extra_lists if l["id"] != list_id]
        self.hidden_lists.add(list_id)

    async def create_label(self, board_id, name, color, position):
        obj = {"id": f"lb{len(self.labels) + 1}", "name": name, "color": color,
               "position": position}
        self.labels.append(obj)
        return obj

    async def update_label(self, label_id, fields):
        for obj in self.labels:
            if obj["id"] == label_id:
                obj.update(fields)
                return obj
        return {}

    async def delete_label(self, label_id):
        self.deleted.append(f"label:{label_id}")
        self.labels = [l for l in self.labels if l["id"] != label_id]

    async def remove_label(self, card_id, label_id):
        self.card_labels = [
            cl for cl in self.card_labels
            if not (cl["cardId"] == card_id and cl["labelId"] == label_id)
        ]

    async def update_container(self, project_id, fields):
        self.calls.append(f"update_container:{project_id}:{','.join(sorted(fields))}")
        return {"id": project_id, **fields}

    async def create_container(self, name, container_type="shared"):
        self.calls.append(f"create_container:{name}:{container_type}")
        return {"id": f"container-{len(self.calls)}", "name": name}

    async def create_board(self, project_id, name, position, board_type="project"):
        return {"id": f"board-{position}", "projectId": project_id, "name": name,
                "type": board_type}

    async def update_board(self, board_id, fields):
        self.calls.append(f"update_board:{board_id}:{','.join(sorted(fields))}")
        return {"id": board_id, **fields}

    # ---- people ----
    async def add_board_membership(self, board_id, user_id, role, can_comment=None):
        existing = next((m for m in self.board_memberships
                         if m["boardId"] == board_id and m["userId"] == user_id), None)
        if existing:
            return 409, {"code": "E_CONFLICT"}
        self.board_memberships.append({"id": f"bm{len(self.board_memberships) + 1}",
                                       "boardId": board_id, "userId": user_id, "role": role})
        self.calls.append(f"add_member:{user_id}:{role}")
        return 200, {"item": self.board_memberships[-1]}

    async def update_board_membership(self, membership_id, fields):
        for m in self.board_memberships:
            if m["id"] == membership_id:
                m.update(fields)
                return m
        return {}

    async def remove_board_membership(self, membership_id):
        self.calls.append(f"remove_member:{membership_id}")
        self.board_memberships = [m for m in self.board_memberships
                                  if m["id"] != membership_id]

    async def set_instance_role(self, user_id, role):
        for u in self.directory:
            if u["id"] == user_id:
                u["role"] = role
                self.calls.append(f"set_role:{user_id}:{role}")
                return u
        return {}

    async def create_api_key(self, user_id):
        self.calls.append(f"mint_key:{user_id}")
        for u in self.directory:
            if u["id"] == user_id:
                self.minted += 1
                u["apiKeyPrefix"] = f"pfx{self.minted}"
                return u, f"secret-key-{self.minted}"
        return {}, None

    async def revoke_api_key(self, user_id):
        self.calls.append(f"revoke_key:{user_id}")
        for u in self.directory:
            if u["id"] == user_id:
                u["apiKeyPrefix"] = None
                return u
        return {}

    async def add_project_manager(self, project_id, user_id):
        self.project_managers.append({"projectId": project_id, "userId": user_id})
        return 200, {"item": self.project_managers[-1]}


def setup_fake(**config_kw: Any) -> FakePlanka:
    fake = FakePlanka()
    A._config = Config(base_url="https://example.test", api_key="x", **config_kw)
    A._client = fake  # type: ignore[assignment]
    return fake


def run(coro):
    import asyncio
    return asyncio.run(coro)
