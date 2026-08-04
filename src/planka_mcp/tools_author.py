"""Creating and refining tasks."""

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

@mcp.tool(annotations=WRITE)
@tool_result
async def create_task(
    title: Annotated[str, Field(description="Short imperative title for the new task.")],
    description: Annotated[
        str | None, Field(description="What needs doing, and how to tell it is done.")
    ] = None,
    board_id: Annotated[
        str | None,
        Field(description="Board to create it on. Required when more than one board "
                          "is in scope."),
    ] = None,
    due_date: Annotated[
        str | None, Field(description="Deadline as ISO-8601, e.g. 2026-08-12T17:00:00Z.")
    ] = None,
    labels: Annotated[
        list[str] | None,
        Field(description="Existing board label names to apply, e.g. ['priority: high']. "
                          "Unknown names are reported back, not created."),
    ] = None,
    checklist: Annotated[
        list[str] | None, Field(description="Optional checklist items for the new task.")
    ] = None,
) -> dict[str, Any]:
    """Create a new task in the todo stage of a board - follow-up work you
    discovered, or a task you were asked to file. It lands unclaimed, so claim it
    before working on it. Labels must already exist on the board; this tool never
    invents board structure."""
    config, client = await _get_client()
    board = await _resolve_board(config, client, board_id)
    if not isinstance(board, BoardView):  # an explanation of why the board is unclear
        return board
    view = board

    todo_list_id = view.list_of_status.get(TODO)
    if not todo_list_id:
        return {"ok": False, "result": "no_list_for_status",
                "reason": f"Board '{view.board_name}' has no list mapped to 'todo'.",
                "known_stages": sorted(view.list_of_status)}

    if due_date and parse_due(due_date) is None:
        return {"ok": False, "error": f"Could not read due_date '{due_date}'. "
                                      "Use ISO-8601, e.g. 2026-08-12T17:00:00Z."}

    positions = [
        c.get("position") for c in view.cards.values()
        if str(c.get("listId")) == todo_list_id
    ]
    fields: dict[str, Any] = {"name": title.strip(), "position": next_position(positions)}
    if description:
        fields["description"] = description
    if due_date:
        fields["dueDate"] = due_date
    card = await client.create_card(todo_list_id, fields)
    card_id = str(card.get("id"))

    applied, missing = await _apply_labels(client, view, card_id, labels)
    if checklist:
        task_list = await client.create_task_list(card_id, "Checklist", 65536)
        for i, item in enumerate(checklist):
            await client.create_task(str(task_list.get("id")), item, (i + 1) * 65536)
    client.invalidate_board(view.board_id)

    result = {
        "ok": True,
        "result": "created",
        "task_id": card_id,
        "title": card.get("name"),
        "board": view.board_name,
        "status": TODO,
        "labels_applied": applied,
        "checklist_items": len(checklist or []),
        "next_step": "Call claim_task(task_id) before working on it.",
    }
    if missing:
        result["labels_not_found"] = missing
        result["labels_available"] = sorted(
            l.get("name") or "" for l in view.labels.values()
        )
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def update_task_details(
    task_id: Annotated[str, Field(description="Task id to edit.")],
    description: Annotated[
        str | None, Field(description="Replacement description. Omit to leave it alone.")
    ] = None,
    due_date: Annotated[
        str | None, Field(description="New deadline as ISO-8601. Omit to leave it alone.")
    ] = None,
    labels: Annotated[
        list[str] | None,
        Field(description="Existing board label names to add. Labels are never removed."),
    ] = None,
) -> dict[str, Any]:
    """Refine a task's description, deadline or labels - for example after
    investigating and learning what the work actually involves. Only tasks you
    hold, or tasks nobody has claimed, can be edited; another worker's task is
    never touched."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    guard = _require_not_someone_elses(view, memberships, my_id)
    if guard:
        return guard

    if due_date and parse_due(due_date) is None:
        return {"ok": False, "error": f"Could not read due_date '{due_date}'. "
                                      "Use ISO-8601, e.g. 2026-08-12T17:00:00Z."}

    fields: dict[str, Any] = {}
    if description is not None:
        fields["description"] = description
    if due_date is not None:
        fields["dueDate"] = due_date
    if fields:
        await client.patch_card(task_id, fields)
    applied, missing = await _apply_labels(client, view, task_id, labels)
    client.invalidate_board(view.board_id)

    if not fields and not applied and not missing:
        return {"ok": True, "result": "nothing_to_change", "task_id": task_id}
    result = {"ok": True, "result": "updated", "task_id": task_id,
              "changed": sorted(fields), "labels_applied": applied}
    if missing:
        result["labels_not_found"] = missing
        result["labels_available"] = sorted(
            l.get("name") or "" for l in view.labels.values()
        )
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def update_checklist(
    task_id: Annotated[str, Field(description="Task id whose checklist to update.")],
    add_items: Annotated[
        list[str] | None, Field(description="New checklist items to append.")
    ] = None,
    complete_items: Annotated[
        list[str] | None,
        Field(description="Existing checklist item names to tick off (case-insensitive)."),
    ] = None,
    reopen_items: Annotated[
        list[str] | None, Field(description="Checklist item names to un-tick.")
    ] = None,
) -> dict[str, Any]:
    """Break a task into checklist steps and tick them off as you go, so progress
    is visible on the board while you work. Only tasks you hold, or unclaimed
    ones, can be changed. Items are never deleted - reopen them instead."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    guard = _require_not_someone_elses(view, memberships, my_id)
    if guard:
        return guard

    existing = [t for t in view.tasks_of_card.get(task_id, []) if not t.get("linkedCardId")]
    by_name = {(t.get("name") or "").strip().lower(): t for t in existing}
    added: list[str] = []
    changed: list[str] = []
    unmatched: list[str] = []

    if add_items:
        task_list_id = next(
            (str(tl["id"]) for tl in view.task_lists.values()
             if str(tl.get("cardId")) == task_id),
            None,
        )
        if task_list_id is None:
            task_list_id = str((await client.create_task_list(task_id, "Checklist", 65536)).get("id"))
        base = next_position([t.get("position") for t in existing])
        for i, item in enumerate(add_items):
            await client.create_task(task_list_id, item, base + i * 65536)
            added.append(item)

    for names, completed in ((complete_items, True), (reopen_items, False)):
        for name in names or []:
            task = by_name.get(name.strip().lower())
            if task is None:
                unmatched.append(name)
                continue
            if bool(task.get("isCompleted")) != completed:
                await client.set_task_completed(str(task["id"]), completed)
                changed.append(name)

    client.invalidate_board(view.board_id)
    result = {"ok": True, "result": "checklist_updated", "task_id": task_id,
              "added": added, "toggled": changed}
    if unmatched:
        result["items_not_found"] = unmatched
        result["existing_items"] = [t.get("name") for t in existing]
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def attach_link(
    task_id: Annotated[str, Field(description="Task id to attach the link to.")],
    url: Annotated[str, Field(description="http/https URL of the deliverable.")],
    name: Annotated[str, Field(description="Human-readable label for the link.")],
) -> dict[str, Any]:
    """Attach a link to a task - a pull request, document or dashboard you
    produced. Use this alongside `add_result` when the output lives somewhere
    else. Only tasks you hold, or unclaimed ones, can be changed."""
    config, client, card, view, memberships, my_id = await _load_card(task_id, fresh=False)
    guard = _require_not_someone_elses(view, memberships, my_id)
    if guard:
        return guard
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "Only http:// and https:// links can be attached."}

    attachment = await client.attach_link(task_id, url.strip(), name.strip())
    return {"ok": True, "result": "attached", "task_id": task_id,
            "attachment_id": attachment.get("id"), "name": name}


