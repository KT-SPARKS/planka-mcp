"""Shared application core: the MCP server object, config/client access,
the capability layer, and helpers every tool group uses.

Design rule: tools are decision-shaped, not endpoint-shaped. The model never
sees routes, tokens, list ids or pagination - it sees tasks, claims and status.
Guardrails (no delete, no touching other people's cards, legal transitions only,
atomic claims) are enforced here in code, not in prose.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from typing import Annotated, Any, Awaitable, Callable

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import PlankaClient, PlankaError
from .config import Config, ConfigError, load_config
from .roles import (
    ADMIN,
    ASSIGN,
    AUTHOR,
    PEOPLE,
    READ,
    STRUCTURE,
    WORK,
    Effective,
    denial,
    resolve_effective_role,
)
from .domain import (
    DONE,
    IN_PROGRESS,
    REOPEN_TRANSITIONS,
    REVIEW,
    STATUSES,
    TODO,
    TRANSITIONS,
    BoardView,
    next_position,
    parse_due,
    sort_key,
)

INSTRUCTIONS = """\
Planka work queue and board management for an autonomous agent.

Vocabulary: a **project** is the tab you work in (lists, cards, members). A
**board** is the container holding several projects. Task = card.

Doing work:
1. Call `list_actionable_tasks` first. It already filters to work that is ready -
   do not reason about what is blocked from raw board data.
2. Claim a task with `claim_task` before doing any work on it. If the claim comes
   back already_taken, that task belongs to someone else - pick another.
3. `get_task` for detail, `update_status` to move it along (todo -> in_progress ->
   review/done). Only legal transitions are accepted.
4. Record output with `add_result` (and `attach_link` for anything living
   elsewhere) before marking it done. `release_task` hands work back.

Answering questions about the workspace:
* `find_tasks` searches every project you can see, including other people's work -
  by text, assignee ("me", "unassigned" or a name), status, label or overdue. Use
  it for "what is Ada working on", "what is overdue", "where is the login card".
  Searching for a person covers all three ways work reaches them: the task itself,
  a checklist item assigned to them, and being mentioned in a comment. Report what
  `matched_by` says rather than assuming they own the whole task - being mentioned
  is not the same as being assigned.
  `list_actionable_tasks` is narrower on purpose: it is only your own ready queue.

Managing a project:
* `list_projects` / `get_project` show structure, labels, members and the role
  you hold. Check `get_project` before changing anything.
* `create_task`, `update_task_details`, `set_task_labels`, `update_checklist` and
  `move_task` cover task content. Priority and size are labels, so changing
  priority means changing labels.
* `create_list`, `update_list`, `delete_list` and `manage_labels` shape the
  project. Retire a stage by setting its type to `inactive`; deletion only ever
  applies to something empty and unused.
* To start something new: `create_board` makes a container, `create_project`
  makes a tab inside one. When an existing project is the template, use
  `copy_project_structure` rather than retyping its columns - it carries each
  column's type across, which naming cannot, and can bring labels and members
  too. It copies structure only, never cards.
* Anything placed in a list of type `closed` is marked finished by Planka.

People:
* `assign_people` puts someone on a task and, if they are not on the project yet,
  adds them - as a worker unless told otherwise. Resolve ambiguity with
  `list_people` rather than guessing which person was meant.
* Teams often hand work over by writing "Assigned to: @[Name](id)" in a comment.
  In Planka that is only text - the person is not assigned, so the task never
  reaches their queue and no report counts it as theirs. `find_informal_assignments`
  lists those comments and the `assign_people` call that would make each one real.
  Report them; do not assign on your own initiative, because a mention can be a
  question rather than a hand-off. Ask the human first.
* `set_project_members` changes who is on a project and the role they hold:
  worker, editor or guest.

Your rights are checked per project and per action. `whoami` reports what this
server may do. When a call comes back not_permitted, say so plainly - do not try
to reach the same outcome another way.

An API key is a credential that acts as a person. If you mint one, hand the value
over deliberately and say plainly that it appears in this transcript.

Rules enforced by this server: nothing that holds work is ever deleted, you can
only claim or release tasks for yourself, a task claimed by someone else is never
edited or moved, status transitions are validated, and instance-level user
administration is disabled unless the operator turned it on.\
"""

mcp = MCPServer(
    "planka-tasks",
    title="Planka task queue",
    instructions=INSTRUCTIONS,
    version="0.2.0",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

_config: Config | None = None
_client: PlankaClient | None = None
_lock = asyncio.Lock()


async def _get_client() -> tuple[Config, PlankaClient]:
    global _config, _client
    async with _lock:
        if _client is None:
            _config = load_config()
            _client = PlankaClient(_config)
    assert _config is not None and _client is not None
    return _config, _client


def tool_result(func: Callable[..., Awaitable[dict[str, Any]]]):
    """Turn every failure into a clean tool result instead of an exception."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await func(*args, **kwargs)
        except (PlankaError, ConfigError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never crash a tool call
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


async def _load_card(
    task_id: str, *, fresh: bool = True
) -> tuple[Config, PlankaClient, dict[str, Any], BoardView, list[dict[str, Any]], str]:
    """Fetch a card plus its board context, enforcing the board allowlist."""
    config, client = await _get_client()
    payload = await client.card(task_id)
    card = payload.get("item") or {}
    if not card:
        raise PlankaError(f"No task with id {task_id}")

    board_id = str(card.get("boardId") or "")
    if not config.board_allowed(board_id):
        raise PlankaError(
            f"Task {task_id} is on a board this server is not allowed to touch"
        )

    view = BoardView(await client.board(board_id, fresh=fresh), config.status_lists)
    memberships = (payload.get("included") or {}).get("cardMemberships")
    if memberships is None:
        memberships = [
            {"cardId": task_id, "userId": user_id}
            for user_id in view.members_of_card.get(task_id, [])
        ]
    my_id = await client.my_id()
    # Board graph may be a few seconds stale; the card payload is authoritative.
    view.cards[task_id] = card
    view.members_of_card[task_id] = [str(m.get("userId")) for m in memberships]
    return config, client, card, view, memberships, my_id


def _member_names(view: BoardView, user_ids: list[str]) -> list[str]:
    return [(view.users.get(uid) or {}).get("name") or uid for uid in user_ids]


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


async def _resolve_board(
    config: Config, client: PlankaClient, board_id: str | None
) -> BoardView | dict[str, Any]:
    """Pick the board to write to, or explain why the choice is ambiguous."""
    if board_id and not config.board_allowed(board_id):
        return {"ok": False, "error": "That board is outside this server's allowlist."}
    if not board_id:
        candidates = await client.board_ids()
        if len(candidates) != 1:
            names = []
            for candidate in candidates[:20]:
                try:
                    view = BoardView(await client.board(candidate), config.status_lists)
                    names.append({"board_id": candidate, "name": view.board_name})
                except PlankaError:
                    continue
            return {"ok": False, "result": "board_id_required",
                    "reason": "More than one board is in scope; say which one.",
                    "boards": names}
        board_id = candidates[0]
    return BoardView(await client.board(board_id, fresh=True), config.status_lists)


async def _apply_labels(
    client: PlankaClient, view: BoardView, card_id: str, labels: list[str] | None
) -> tuple[list[str], list[str]]:
    """Add existing board labels by name. Never creates or removes labels."""
    if not labels:
        return [], []
    by_name = {
        (obj.get("name") or "").strip().lower(): str(obj["id"])
        for obj in view.labels.values()
    }
    applied: list[str] = []
    missing: list[str] = []
    for label in labels:
        label_id = by_name.get(label.strip().lower())
        if label_id is None:
            missing.append(label)
            continue
        await client.add_label(card_id, label_id)
        applied.append(label)
    return applied, missing


def _require_not_someone_elses(
    view: BoardView, memberships: list[dict[str, Any]], my_id: str
) -> dict[str, Any] | None:
    holders = [str(m.get("userId")) for m in memberships]
    others = [u for u in holders if u != my_id]
    if others:
        return {
            "ok": False,
            "result": "not_yours",
            "reason": f"This task is claimed by {', '.join(_member_names(view, others))}.",
            "next_step": "Leave it alone and pick different work.",
        }
    return None


def _allowed_next(current: str, config: Config) -> set[str]:
    allowed = set(TRANSITIONS.get(current, set()))
    if current == DONE and config.allow_reopen:
        allowed |= REOPEN_TRANSITIONS
    return allowed




# --------------------------------------------------------------------------
# capability layer
# --------------------------------------------------------------------------


async def effective_role(board_id: str) -> Effective:
    """The role in force on one board (a "project" in the user's vocabulary)."""
    config, client = await _get_client()
    me = await client.me()
    my_id = str(me.get("id") or "")

    view = BoardView(await client.board(board_id), config.status_lists)
    graph = await client.project_graph()
    included = graph.get("included") or {}
    managers = {
        str(m.get("userId"))
        for m in included.get("projectManagers") or []
        if str(m.get("projectId")) == view.project_id
    }
    memberships = view.board_memberships or [
        m for m in included.get("boardMemberships") or []
        if str(m.get("boardId")) == board_id
    ]
    return resolve_effective_role(
        user_id=my_id,
        instance_role=me.get("role"),
        board_memberships=memberships,
        project_manager_user_ids=managers,
        board_id=board_id,
        act_as=config.act_as,
    )


async def require(board_id: str, capability: str, what: str) -> dict[str, Any] | None:
    """Refuse, with a reason the model can act on, when the role is too low."""
    role = await effective_role(board_id)
    if role.can(capability):
        return None
    return denial(role, capability, what)
