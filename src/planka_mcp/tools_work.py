"""Finding, claiming and progressing work."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .app import (
    IDEMPOTENT_WRITE,
    READ_ONLY,
    WRITE,
    _allowed_next,
    _apply_labels,
    _clean,
    _get_client,
    _load_card,
    _member_names,
    _require_not_someone_elses,
    _resolve_board,
    mcp,
    require,
    tool_result,
)
from .client import PlankaError
from .config import Config
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
from .roles import AUTHOR, WORK



@mcp.tool(annotations=READ_ONLY)
@tool_result
async def list_actionable_tasks(
    board_id: Annotated[
        str | None,
        Field(description="Restrict to one board id. Omit to scan every accessible board."),
    ] = None,
    search: Annotated[
        str | None,
        Field(description="Optional case-insensitive text filter on title and description."),
    ] = None,
    limit: Annotated[
        int, Field(description="Maximum number of tasks to return (1-50).", ge=1, le=50)
    ] = 20,
) -> dict[str, Any]:
    """List tasks that are genuinely ready to be worked on, most urgent first.

    Call this first, before any other tool. The filtering happens on the server:
    only cards sitting in a 'todo' stage of an active list, not closed, and
    either unassigned or already assigned to you are returned. Each entry carries
    what you need to choose: title, priority, due date, effort, labels, subtask
    counts and a description excerpt. Pick one, then call `claim_task`.
    """
    config, client = await _get_client()
    my_id = await client.my_id()

    board_ids = [board_id] if board_id else await client.board_ids()
    board_ids = [b for b in board_ids if config.board_allowed(b)]
    if not board_ids:
        return {
            "ok": True,
            "tasks": [],
            "note": "No boards are visible to this account (or the allowlist excludes them all).",
        }

    payloads = await asyncio.gather(
        *(client.board(b) for b in board_ids), return_exceptions=True
    )

    tasks: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    unreachable: list[str] = []
    needle = (search or "").lower().strip()

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for board_key, payload in zip(board_ids, payloads):
        if isinstance(payload, BaseException):
            unreachable.append(str(board_key))
            continue
        view = BoardView(payload, config.status_lists)
        for card_id, card in view.cards.items():
            if card.get("isClosed"):
                skip("closed")
                continue
            if view.status_of_card(card) != TODO:
                skip("not in a todo stage")
                continue
            members = view.members_of_card.get(card_id, [])
            if members and my_id not in members:
                skip("assigned to someone else")
                continue
            labels_lower = [l.lower() for l in view.labels_of_card.get(card_id, [])]
            if config.blocked_labels and any(
                b in labels_lower for b in config.blocked_labels
            ):
                skip("blocked label")
                continue
            blocking = view.blocking_deps(card_id) if config.require_deps_met else []
            if blocking:
                skip("waiting on a linked task")
                continue
            item = view.summarize(card, my_id)
            if needle and needle not in (
                f"{item['title'] or ''} {item['description']}".lower()
            ):
                continue
            tasks.append(item)

    tasks.sort(key=sort_key)
    result: dict[str, Any] = {
        "ok": True,
        "count": min(len(tasks), limit),
        "total_ready": len(tasks),
        "tasks": [_clean(t) for t in tasks[:limit]],
        "filtered_out": skipped,
        "next_step": "Call claim_task(task_id) for the one you choose before starting work.",
    }
    if unreachable:
        result["unreachable_boards"] = unreachable
    return result


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def get_task(
    task_id: Annotated[str, Field(description="Task id from list_actionable_tasks.")],
) -> dict[str, Any]:
    """Full detail for one task: description, labels, subtasks, assignees,
    comments and anything it is waiting on. Use it after you claim a task, to
    understand what has to be done."""
    _, client, card, view, memberships, my_id = await _load_card(task_id, fresh=False)
    item = view.summarize(card, my_id)
    tasks = [
        t for t in view.tasks_of_card.get(task_id, []) if not t.get("linkedCardId")
    ]
    comments = await client.comments(task_id)
    detail = _clean(item)
    detail["description"] = card.get("description") or ""
    detail["subtask_items"] = [
        {"name": t.get("name"), "done": bool(t.get("isCompleted"))} for t in tasks
    ]
    detail["waiting_on"] = view.blocking_deps(task_id)
    detail["assignees"] = _member_names(view, view.members_of_card.get(task_id, []))
    detail["recent_comments"] = [
        {
            "author": (view.users.get(str(c.get("userId"))) or {}).get("name"),
            "text": (c.get("text") or "")[:500],
            "at": c.get("createdAt"),
        }
        for c in comments[:10]
    ]
    detail["allowed_next_status"] = sorted(
        _allowed_next(detail["status"], (await _get_client())[0])
    )
    return {"ok": True, "task": detail}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
@tool_result
async def claim_task(
    task_id: Annotated[
        str, Field(description="Task id to claim, taken from list_actionable_tasks.")
    ],
) -> dict[str, Any]:
    """Claim a task for yourself before working on it. This is atomic: the claim
    is a unique assignment row in Planka, so a second worker racing for the same
    task gets `already_taken` back and should simply pick another task. Claiming
    a task you already hold succeeds again without side effects. You can only
    ever claim a task for yourself, never for another user."""
    _, client, card, view, memberships, my_id = await _load_card(task_id)

    status = view.status_of_card(card) or "unmapped"
    if status == DONE:
        return {"ok": False, "result": "not_claimable",
                "reason": "This task is already done.", "task_id": task_id}
    if status == "unmapped":
        return {"ok": False, "result": "not_claimable",
                "reason": "This task sits in a list that is not a work stage "
                          "(archive/trash or an unmapped list).", "task_id": task_id}

    holders = [str(m.get("userId")) for m in memberships]
    others = [u for u in holders if u != my_id]
    if others:
        return {
            "ok": False,
            "result": "already_taken",
            "task_id": task_id,
            "held_by": _member_names(view, others),
            "next_step": "Pick a different task from list_actionable_tasks.",
        }

    http_status, _ = await client.add_membership(task_id, my_id)
    client.invalidate_board(str(card.get("boardId")))

    # Verify after write: another worker may have written between our read and
    # our POST. Planka has no compare-and-swap on plain fields, so we re-read and
    # yield to whoever got the earlier row.
    payload = await client.card(task_id)
    fresh_memberships = (payload.get("included") or {}).get("cardMemberships") or []
    mine = next((m for m in fresh_memberships if str(m.get("userId")) == my_id), None)
    rivals = [m for m in fresh_memberships if str(m.get("userId")) != my_id]
    earlier = [
        m for m in rivals
        if mine and str(m.get("createdAt") or "") < str(mine.get("createdAt") or "")
    ]
    if earlier:
        await client.remove_membership(task_id, my_id)
        client.invalidate_board(str(card.get("boardId")))
        return {
            "ok": False,
            "result": "already_taken",
            "task_id": task_id,
            "held_by": _member_names(view, [str(m.get("userId")) for m in earlier]),
            "next_step": "Pick a different task from list_actionable_tasks.",
        }

    return {
        "ok": True,
        "result": "claimed" if http_status == 200 else "already_yours",
        "task_id": task_id,
        "title": card.get("name"),
        "status": status,
        "next_step": "Call update_status(task_id, 'in_progress') when you start, "
                     "then get_task for the details.",
    }


@mcp.tool(annotations=IDEMPOTENT_WRITE)
@tool_result
async def update_status(
    task_id: Annotated[str, Field(description="Task id you currently hold a claim on.")],
    status: Annotated[
        str,
        Field(description="Target status: todo, in_progress, review or done."),
    ],
) -> dict[str, Any]:
    """Move a task you have claimed to a new status. Legal moves are
    todo -> in_progress -> review -> done (plus in_progress -> todo to hand work
    back). Illegal jumps are rejected. You must hold the claim on the task, and
    the board must have a list for the target status."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)

    target = (status or "").strip().lower().replace(" ", "_").replace("-", "_")
    if target not in STATUSES:
        return {"ok": False, "error": f"Unknown status '{status}'.",
                "valid_statuses": list(STATUSES)}

    holders = [str(m.get("userId")) for m in memberships]
    if my_id not in holders:
        return {
            "ok": False,
            "result": "not_yours",
            "reason": "You do not hold the claim on this task."
                      + (f" It is held by {', '.join(_member_names(view, holders))}." if holders else ""),
            "next_step": "Call claim_task(task_id) first." if not holders else
                         "Pick a different task from list_actionable_tasks.",
        }

    current = view.status_of_card(card) or "unmapped"
    if current == target:
        return {"ok": True, "result": "already_in_status", "task_id": task_id,
                "status": target}
    allowed = _allowed_next(current, config)
    if target not in allowed:
        return {
            "ok": False,
            "result": "illegal_transition",
            "reason": f"{current} -> {target} is not allowed.",
            "allowed_next_status": sorted(allowed),
        }

    target_list_id = view.list_of_status.get(target)
    if not target_list_id:
        return {
            "ok": False,
            "result": "no_list_for_status",
            "reason": f"Board '{view.board_name}' has no list mapped to '{target}'.",
            "known_stages": sorted(view.list_of_status),
        }

    positions = [
        c.get("position")
        for cid, c in view.cards.items()
        if str(c.get("listId")) == target_list_id
    ]
    await client.move_card(task_id, target_list_id, next_position(positions))
    client.invalidate_board(str(card.get("boardId")))

    return {
        "ok": True,
        "result": "status_updated",
        "task_id": task_id,
        "from": current,
        "to": target,
        "list": (view.lists.get(target_list_id) or {}).get("name"),
        "next_step": "Record what you produced with add_result before finishing."
        if target != DONE
        else "Task complete. Call list_actionable_tasks for the next one.",
    }


@mcp.tool(annotations=WRITE)
@tool_result
async def add_result(
    task_id: Annotated[str, Field(description="Task id you hold a claim on.")],
    note: Annotated[
        str,
        Field(description="What you produced: summary, links, file paths, findings."),
    ],
    idempotency_key: Annotated[
        str | None,
        Field(description="Optional key. Re-posting with the same key is a no-op, "
                          "so a retried call cannot duplicate the result."),
    ] = None,
) -> dict[str, Any]:
    """Attach your output to a task: what you did, what you produced, where it
    lives. Post this before moving the task to done, so the work is auditable.
    Requires that you hold the claim."""
    _, client, card, view, memberships, my_id = await _load_card(task_id, fresh=False)
    if my_id not in [str(m.get("userId")) for m in memberships]:
        return {"ok": False, "result": "not_yours",
                "reason": "You do not hold the claim on this task.",
                "next_step": "Call claim_task(task_id) first."}

    marker = f"<!-- planka-mcp:result:{idempotency_key} -->" if idempotency_key else ""
    if marker:
        existing = await client.comments(task_id)
        if any(marker in (c.get("text") or "") for c in existing):
            return {"ok": True, "result": "already_recorded", "task_id": task_id}

    text = f"**Agent result**\n\n{note.strip()}"
    if marker:
        text += f"\n\n{marker}"
    created = await client.create_comment(task_id, text)
    return {"ok": True, "result": "recorded", "task_id": task_id,
            "comment_id": created.get("id")}


@mcp.tool(annotations=WRITE)
@tool_result
async def add_comment(
    task_id: Annotated[str, Field(description="Task id to comment on.")],
    text: Annotated[str, Field(description="Comment body, plain text or markdown.")],
) -> dict[str, Any]:
    """Leave a comment on a task - a question, a blocker, or progress context for
    the humans watching the board. Use `add_result` for the actual deliverable."""
    _, client, card, view, _, _ = await _load_card(task_id, fresh=False)
    created = await client.create_comment(task_id, text.strip())
    return {"ok": True, "result": "commented", "task_id": task_id,
            "comment_id": created.get("id")}


@mcp.tool(annotations=IDEMPOTENT_WRITE)
@tool_result
async def release_task(
    task_id: Annotated[str, Field(description="Task id you claimed and cannot finish.")],
    reason: Annotated[
        str | None,
        Field(description="Optional note posted to the task explaining why."),
    ] = None,
    move_back_to_todo: Annotated[
        bool,
        Field(description="Also move the task back to the todo stage. Default true."),
    ] = True,
) -> dict[str, Any]:
    """Give up your claim on a task so another worker can take it. Only your own
    claim is ever removed - other people's assignments are untouched. Nothing is
    deleted; the task and its history stay on the board."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    if my_id not in [str(m.get("userId")) for m in memberships]:
        return {"ok": False, "result": "not_yours",
                "reason": "You do not hold the claim on this task."}

    if reason:
        await client.create_comment(task_id, f"**Agent released this task**\n\n{reason.strip()}")

    moved_to = None
    current = view.status_of_card(card) or "unmapped"
    todo_list_id = view.list_of_status.get(TODO)
    if move_back_to_todo and current in (IN_PROGRESS, REVIEW) and todo_list_id:
        positions = [
            c.get("position") for c in view.cards.values()
            if str(c.get("listId")) == todo_list_id
        ]
        await client.move_card(task_id, todo_list_id, next_position(positions))
        moved_to = TODO

    await client.remove_membership(task_id, my_id)
    client.invalidate_board(str(card.get("boardId")))
    return {"ok": True, "result": "released", "task_id": task_id,
            "moved_to": moved_to,
            "next_step": "Call list_actionable_tasks to pick different work."}


