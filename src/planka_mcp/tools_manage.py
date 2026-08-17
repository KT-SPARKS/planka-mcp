"""Structure: boards, projects (tabs), lists and labels.

Vocabulary: a **project** here is what you work in - a tab, which Planka calls a
board. A **board** is the container holding those tabs, which Planka calls a
project. The tools speak the first language; the client speaks the second.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import BaseModel, Field

from .app import (
    READ_ONLY,
    WRITE,
    _clean,
    _get_client,
    _load_card,
    _require_not_someone_elses,
    mcp,
    require,
    tool_result,
)
from .client import PlankaError
from .domain import LIST_TYPES, TODO, BoardView, next_position
from .roles import AUTHOR, PEOPLE, STRUCTURE


class Stage(BaseModel):
    """One workflow column, when the caller wants to state the type outright."""

    name: str = Field(description="Column name, e.g. 'Ready for QA'.")
    type: str = Field(
        default="active",
        description="active (work stage) | waiting (on hold) | inactive (out of "
                    "flow) | closed (Planka marks cards here as finished).",
    )


# Lists Planka creates per board. Never copied, never treated as work stages.
SYSTEM_LIST_TYPES = ("inbox", "recurring", "archive", "trash")

LIST_COLORS = (
    "berry-red", "pumpkin-orange", "lagoon-blue", "pink-tulip", "light-mud",
    "orange-peel", "bright-moss", "antique-blue", "dark-granite", "turquoise-sea",
)
BOARD_VIEWS = ("kanban", "grid", "list")
CARD_TYPES = ("project", "story", "link")
PROJECT_TYPES = ("project", "library", "collection")

LABEL_COLORS = (
    "berry-red", "pumpkin-orange", "lagoon-blue", "pink-tulip", "light-mud",
    "orange-peel", "bright-moss", "antique-blue", "dark-granite", "turquoise-sea",
    "midnight-blue", "sunny-grass", "wet-rock", "egg-yellow", "desert-sand",
)


async def _view(board_id: str, *, fresh: bool = False) -> BoardView:
    config, client = await _get_client()
    if not config.board_allowed(board_id):
        raise PlankaError("That project is outside this server's allowlist.")
    return BoardView(await client.board(board_id, fresh=fresh), config.status_lists)


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def list_boards() -> dict[str, Any]:
    """List the boards you can reach - the containers that hold projects. Use it
    to find where a project lives, or before creating a new project."""
    config, client = await _get_client()
    graph = await client.project_graph()
    included = graph.get("included") or {}
    boards_by_project: dict[str, list[dict[str, Any]]] = {}
    for board in included.get("boards") or []:
        boards_by_project.setdefault(str(board.get("projectId")), []).append(board)

    out = []
    for project in graph.get("items") or []:
        project_id = str(project.get("id"))
        tabs = boards_by_project.get(project_id, [])
        out.append({
            "board_id": project_id,
            "name": project.get("name") or "(personal board)",
            "shared": project.get("ownerProjectManagerId") is None,
            "projects": [
                {"project_id": str(b["id"]), "name": b.get("name"),
                 "type": b.get("type") or "project"}
                for b in tabs
            ],
        })
    return {"ok": True, "boards": out,
            "note": "A 'project' is a tab you work in; a 'board' is the container."}


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def list_projects(
    board_id: Annotated[
        str | None, Field(description="Only projects inside this board. Omit for all.")
    ] = None,
) -> dict[str, Any]:
    """List the projects (tabs) you can work in, with the role you hold on each.
    Start here when you are asked to do something on a named project."""
    config, client = await _get_client()
    graph = await client.project_graph()
    included = graph.get("included") or {}
    names = {str(p["id"]): p.get("name") for p in graph.get("items") or []}

    boards = [
        b for b in included.get("boards") or []
        if (not board_id or str(b.get("projectId")) == board_id)
        and config.board_allowed(str(b.get("id")))
    ]
    from .app import effective_role  # local import: avoids a cycle at module load

    async def describe(board: dict[str, Any]) -> dict[str, Any]:
        board_key = str(board["id"])
        entry = {
            "project_id": board_key,
            "name": board.get("name"),
            "type": board.get("type") or "project",
            "board": names.get(str(board.get("projectId"))) or "(personal board)",
            "board_id": str(board.get("projectId")),
        }
        try:
            entry["your_role"] = (await effective_role(board_key)).role
        except PlankaError:
            entry["your_role"] = "unknown"
        return entry

    described = await asyncio.gather(*(describe(b) for b in boards))
    return {"ok": True, "count": len(described), "projects": list(described)}


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def get_project(
    project_id: Annotated[str, Field(description="Project (tab) id from list_projects.")],
) -> dict[str, Any]:
    """Full structure of one project: its lists and what status each maps to, its
    labels and how heavily they are used, its members and their roles, and your
    own role. Read this before changing structure or assigning people."""
    from .app import effective_role

    view = await _view(project_id, fresh=True)
    role = await effective_role(project_id)
    structure = view.structure()
    structure["your_role"] = role.describe()
    structure["list_types_available"] = list(LIST_TYPES)
    structure["board_roles_available"] = ["editor", "worker", "guest"]
    return {"ok": True, "project": structure}


@mcp.tool(annotations=WRITE)
@tool_result
async def create_project(
    board_id: Annotated[str, Field(description="Board (container) to create it in.")],
    name: Annotated[str, Field(description="Name of the new project (tab).")],
    lists: Annotated[
        list[Stage] | list[str] | None,
        Field(description="Workflow stages in order. Either plain names, or "
                          "{name, type} to set each type outright. Plain names are "
                          "typed by convention: a 'done'-style name becomes a closed "
                          "list. Defaults to To Do / In Progress / Review / Done."),
    ] = None,
    project_type: Annotated[
        str,
        Field(description="project (holds work) | library | collection. Only "
                          "'project' projects are scanned for tasks."),
    ] = "project",
    default_view: Annotated[
        str | None, Field(description="How it opens: kanban, grid or list.")
    ] = None,
    default_card_type: Annotated[
        str | None, Field(description="Default kind for new tasks: project, story or link.")
    ] = None,
    expand_checklists: Annotated[
        bool | None, Field(description="Show checklists expanded on the task front.")
    ] = None,
) -> dict[str, Any]:
    """Create a new project (tab) inside a board, pre-populated with workflow
    lists so it is immediately usable. Requires admin rights on that board.

    To reproduce an existing project's layout, use `copy_project_structure`
    instead - it carries the exact list types across, which naming alone cannot."""
    config, client = await _get_client()
    graph = await client.project_graph()
    managers = {
        str(m.get("userId")) for m in (graph.get("included") or {}).get("projectManagers") or []
        if str(m.get("projectId")) == board_id
    }
    me = await client.me()
    if me.get("role") != "admin" and str(me.get("id")) not in managers:
        return {"ok": False, "result": "not_permitted",
                "reason": "Creating a project requires managing its board.",
                "your_instance_role": me.get("role")}

    existing = [
        b for b in (graph.get("included") or {}).get("boards") or []
        if str(b.get("projectId")) == board_id
    ]
    if project_type not in PROJECT_TYPES:
        return {"ok": False, "error": f"project_type must be one of {list(PROJECT_TYPES)}."}
    if default_view and default_view not in BOARD_VIEWS:
        return {"ok": False, "error": f"default_view must be one of {list(BOARD_VIEWS)}."}
    if default_card_type and default_card_type not in CARD_TYPES:
        return {"ok": False, "error": f"default_card_type must be one of {list(CARD_TYPES)}."}

    board = await client.create_board(
        board_id, name.strip(), next_position([b.get("position") for b in existing]),
        project_type,
    )
    settings: dict[str, Any] = {}
    if default_view:
        settings["defaultView"] = default_view
    if default_card_type:
        settings["defaultCardType"] = default_card_type
    if expand_checklists is not None:
        settings["expandTaskListsByDefault"] = expand_checklists
    if settings:
        await client.update_board(str(board["id"]), settings)

    stages = lists or ["To Do", "In Progress", "Review", "Done"]
    made = []
    for i, stage in enumerate(stages):
        if isinstance(stage, Stage):
            stage_name, stage_type = stage.name, stage.type
        elif isinstance(stage, dict):
            stage_name, stage_type = str(stage.get("name")), stage.get("type") or "active"
        else:
            stage_name = str(stage)
            stage_type = (
                "closed"
                if stage_name.strip().lower() in ("done", "closed", "shipped", "completed")
                else "active"
            )
        if stage_type not in LIST_TYPES:
            return {"ok": False, "error": f"'{stage_type}' is not a list type; "
                                          f"use one of {list(LIST_TYPES)}."}
        created = await client.create_list(
            str(board["id"]), stage_name, (i + 1) * 65536, stage_type
        )
        made.append({"list_id": str(created.get("id")), "name": created.get("name"),
                     "type": created.get("type")})
    return {"ok": True, "result": "created", "project_id": str(board.get("id")),
            "name": board.get("name"), "type": project_type, "lists": made,
            "settings": settings or "defaults"}


@mcp.tool(annotations=WRITE)
@tool_result
async def update_project(
    project_id: Annotated[str, Field(description="Project (tab) to rename or retune.")],
    name: Annotated[str | None, Field(description="New name.")] = None,
    default_view: Annotated[
        str | None, Field(description="Default view: kanban, grid or list.")
    ] = None,
    default_card_type: Annotated[
        str | None, Field(description="Default kind for new tasks: project, story or link.")
    ] = None,
    limit_to_default_card_type: Annotated[
        bool | None, Field(description="Allow only the default kind of task.")
    ] = None,
    always_show_task_creator: Annotated[
        bool | None, Field(description="Show who created each task on its front.")
    ] = None,
    expand_checklists: Annotated[
        bool | None, Field(description="Show checklists expanded on the task front.")
    ] = None,
) -> dict[str, Any]:
    """Rename a project (tab) or change how it behaves: default view, the kind of
    task it creates, whether checklists open expanded. Requires structure rights."""
    denied = await require(project_id, STRUCTURE, "rename this project")
    if denied:
        return denied
    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name.strip()
    if default_view:
        if default_view not in BOARD_VIEWS:
            return {"ok": False, "error": f"default_view must be one of {list(BOARD_VIEWS)}."}
        fields["defaultView"] = default_view
    if default_card_type:
        if default_card_type not in CARD_TYPES:
            return {"ok": False, "error": f"default_card_type must be one of {list(CARD_TYPES)}."}
        fields["defaultCardType"] = default_card_type
    if limit_to_default_card_type is not None:
        fields["limitCardTypesToDefaultOne"] = limit_to_default_card_type
    if always_show_task_creator is not None:
        fields["alwaysDisplayCardCreator"] = always_show_task_creator
    if expand_checklists is not None:
        fields["expandTaskListsByDefault"] = expand_checklists
    if not fields:
        return {"ok": True, "result": "nothing_to_change", "project_id": project_id}

    _, client = await _get_client()
    updated = await client.update_board(project_id, fields)
    client.invalidate_board(project_id)
    return {"ok": True, "result": "updated", "project_id": project_id,
            "name": updated.get("name"), "changed": sorted(fields)}


@mcp.tool(annotations=WRITE)
@tool_result
async def create_list(
    project_id: Annotated[str, Field(description="Project (tab) to add the list to.")],
    name: Annotated[str, Field(description="List name, e.g. 'Blocked' or 'Ready for QA'.")],
    list_type: Annotated[
        str,
        Field(description="active (work stage) | waiting (on hold) | inactive "
                          "(out of flow) | closed (cards land here as finished)."),
    ] = "active",
    after: Annotated[
        str | None, Field(description="Place it after this list (name or id).")
    ] = None,
    color: Annotated[
        str | None,
        Field(description="Column colour, e.g. berry-red, lagoon-blue, bright-moss."),
    ] = None,
) -> dict[str, Any]:
    """Add a workflow stage to a project. Note that Planka closes any card placed
    in a `closed` list, so use that type only for a genuine done stage."""
    denied = await require(project_id, STRUCTURE, "add a list")
    if denied:
        return denied
    if list_type not in LIST_TYPES:
        return {"ok": False, "error": f"list_type must be one of {list(LIST_TYPES)}."}

    _, client = await _get_client()
    view = await _view(project_id, fresh=True)
    if view.find_list(name):
        return {"ok": False, "result": "already_exists",
                "reason": f"'{name}' already exists on this project."}

    position = next_position([l.get("position") for l in view.lists.values()])
    if after:
        anchor = view.find_list(after)
        if anchor is None:
            return {"ok": False, "error": f"No list called '{after}' on this project.",
                    "lists": [l.get("name") for l in view.lists.values()]}
        position = (anchor.get("position") or 0) + 32768

    if color and color not in LIST_COLORS:
        return {"ok": False, "error": f"color must be one of {list(LIST_COLORS)}."}

    created = await client.create_list(project_id, name.strip(), position, list_type, color)
    client.invalidate_board(project_id)
    return {"ok": True, "result": "created", "list_id": str(created.get("id")),
            "name": created.get("name"), "type": created.get("type"),
            "color": created.get("color")}


@mcp.tool(annotations=WRITE)
@tool_result
async def update_list(
    project_id: Annotated[str, Field(description="Project (tab) the list belongs to.")],
    list_name_or_id: Annotated[str, Field(description="List to change, by name or id.")],
    name: Annotated[str | None, Field(description="New name.")] = None,
    list_type: Annotated[
        str | None,
        Field(description="New type: active, waiting, inactive or closed. Setting "
                          "'inactive' is how you retire a stage without deleting it."),
    ] = None,
    color: Annotated[str | None, Field(description="New colour for the column.")] = None,
    after: Annotated[
        str | None,
        Field(description="Reorder: place this column after that one (name or id). "
                          "Pass 'first' to move it to the front."),
    ] = None,
) -> dict[str, Any]:
    """Rename a list or change its type. Retiring a stage is a type change to
    `inactive`, which keeps every card intact and reversible."""
    denied = await require(project_id, STRUCTURE, "change a list")
    if denied:
        return denied
    if list_type and list_type not in LIST_TYPES:
        return {"ok": False, "error": f"list_type must be one of {list(LIST_TYPES)}."}

    _, client = await _get_client()
    view = await _view(project_id, fresh=True)
    target = view.find_list(list_name_or_id)
    if target is None:
        return {"ok": False, "error": f"No list called '{list_name_or_id}'.",
                "lists": [l.get("name") for l in view.lists.values()]}

    if color and color not in LIST_COLORS:
        return {"ok": False, "error": f"color must be one of {list(LIST_COLORS)}."}

    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name.strip()
    if list_type:
        fields["type"] = list_type
    if color:
        fields["color"] = color
    if after:
        if after.strip().lower() == "first":
            lowest = min((l.get("position") or 0) for l in view.lists.values())
            fields["position"] = lowest / 2 if lowest else 32768
        else:
            anchor = view.find_list(after)
            if anchor is None:
                return {"ok": False, "error": f"No list called '{after}' to place it after.",
                        "lists": [l.get("name") for l in view.lists.values()]}
            fields["position"] = (anchor.get("position") or 0) + 32768
    if not fields:
        return {"ok": True, "result": "nothing_to_change"}

    updated = await client.update_list(str(target["id"]), fields)
    client.invalidate_board(project_id)
    result = {"ok": True, "result": "updated", "list_id": str(target["id"]),
              "name": updated.get("name"), "type": updated.get("type"),
              "color": updated.get("color"),
              "cards_affected": len(view.cards_in_list(str(target["id"])))}
    if list_type == "closed" and result["cards_affected"]:
        result["warning"] = (
            f"Planka marks all {result['cards_affected']} cards in a closed list as finished."
        )
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def delete_list(
    project_id: Annotated[str, Field(description="Project (tab) the list belongs to.")],
    list_name_or_id: Annotated[str, Field(description="List to delete, by name or id.")],
) -> dict[str, Any]:
    """Delete an empty list. A list holding cards is never deleted - move the
    cards out first, or retire the stage with update_list(list_type='inactive')."""
    denied = await require(project_id, STRUCTURE, "delete a list")
    if denied:
        return denied
    _, client = await _get_client()
    view = await _view(project_id, fresh=True)
    target = view.find_list(list_name_or_id)
    if target is None:
        return {"ok": False, "error": f"No list called '{list_name_or_id}'.",
                "lists": [l.get("name") for l in view.lists.values()]}

    cards = view.cards_in_list(str(target["id"]))
    if cards:
        return {
            "ok": False,
            "result": "not_empty",
            "reason": f"'{target.get('name')}' still holds {len(cards)} card(s); "
                      "nothing that contains work is deleted.",
            "next_step": "Move the cards with move_task, or retire the stage with "
                         "update_list(list_type='inactive').",
        }
    await client.delete_list(str(target["id"]))
    client.invalidate_board(project_id)
    return {"ok": True, "result": "deleted", "list_id": str(target["id"]),
            "name": target.get("name")}


@mcp.tool(annotations=WRITE)
@tool_result
async def manage_labels(
    project_id: Annotated[str, Field(description="Project (tab) whose labels to manage.")],
    create: Annotated[
        list[str] | None,
        Field(description="Label names to create, e.g. ['priority: high', 'size: L']."),
    ] = None,
    rename: Annotated[
        dict[str, str] | None,
        Field(description="Mapping of existing label name -> new name."),
    ] = None,
    delete_unused: Annotated[
        list[str] | None,
        Field(description="Label names to delete. Refused if the label is on any card."),
    ] = None,
    colors: Annotated[
        dict[str, str] | None,
        Field(description="Colour for labels being created or recoloured: "
                          "{label name: colour}. Colours are Planka's own names, e.g. "
                          "berry-red, lagoon-blue, sunny-grass. Unset ones cycle "
                          "through the palette."),
    ] = None,
) -> dict[str, Any]:
    """Create, rename or remove the labels a project uses for priority, size and
    state. A label still applied to cards is never deleted."""
    denied = await require(project_id, STRUCTURE, "manage labels")
    if denied:
        return denied
    _, client = await _get_client()
    view = await _view(project_id, fresh=True)
    created, renamed, deleted, refused = [], [], [], []

    for i, name in enumerate(create or []):
        if view.find_label(name):
            refused.append({"label": name, "why": "already exists"})
            continue
        color = (colors or {}).get(name) or LABEL_COLORS[
            (len(view.labels) + i) % len(LABEL_COLORS)
        ]
        label = await client.create_label(
            project_id, name.strip(), color,
            next_position([l.get("position") for l in view.labels.values()]) + i * 65536,
        )
        created.append({"label_id": str(label.get("id")), "name": label.get("name"),
                        "color": label.get("color")})

    for old, new in (rename or {}).items():
        target = view.find_label(old)
        if target is None:
            refused.append({"label": old, "why": "no such label"})
            continue
        changes: dict[str, Any] = {"name": new.strip()}
        if (colors or {}).get(old) or (colors or {}).get(new):
            changes["color"] = (colors or {}).get(new) or (colors or {})[old]
        await client.update_label(str(target["id"]), changes)
        renamed.append({"from": old, "to": new, "color": changes.get("color")})

    for name in delete_unused or []:
        target = view.find_label(name)
        if target is None:
            refused.append({"label": name, "why": "no such label"})
            continue
        in_use = view.cards_of_label.get(str(target["id"]), [])
        if in_use:
            refused.append({"label": name, "why": f"still on {len(in_use)} card(s)"})
            continue
        await client.delete_label(str(target["id"]))
        deleted.append(name)

    for name, color in (colors or {}).items():
        if any(c["name"] == name for c in created) or any(r["from"] == name for r in renamed):
            continue
        target = view.find_label(name)
        if target is None:
            refused.append({"label": name, "why": "no such label to recolour"})
            continue
        await client.update_label(str(target["id"]), {"color": color})
        renamed.append({"from": name, "to": name, "color": color})

    client.invalidate_board(project_id)
    result = {"ok": True, "result": "labels_updated", "created": created,
              "renamed": renamed, "deleted": deleted}
    if refused:
        result["refused"] = refused
        result["existing_labels"] = [l.get("name") for l in view.labels.values()]
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def set_task_labels(
    task_id: Annotated[str, Field(description="Task to relabel.")],
    add: Annotated[list[str] | None, Field(description="Label names to apply.")] = None,
    remove: Annotated[list[str] | None, Field(description="Label names to take off.")] = None,
) -> dict[str, Any]:
    """Apply or take labels off a task - how you change its priority or size,
    since Planka stores both as labels. Labels themselves are untouched; only
    their link to this task changes."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    denied = await require(view.board_id, AUTHOR, "relabel a task")
    if denied:
        return denied
    guard = _require_not_someone_elses(view, memberships, my_id)
    if guard:
        return guard

    applied, removed, unknown = [], [], []
    for name in add or []:
        label = view.find_label(name)
        if label is None:
            unknown.append(name)
            continue
        await client.add_label(task_id, str(label["id"]))
        applied.append(label.get("name"))
    for name in remove or []:
        label = view.find_label(name)
        if label is None:
            unknown.append(name)
            continue
        await client.remove_label(task_id, str(label["id"]))
        removed.append(label.get("name"))

    client.invalidate_board(view.board_id)
    result = {"ok": True, "result": "labels_set", "task_id": task_id,
              "added": applied, "removed": removed}
    if unknown:
        result["labels_not_found"] = unknown
        result["labels_available"] = [l.get("name") for l in view.labels.values()]
        result["next_step"] = "Create it first with manage_labels(create=[...])."
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def move_task(
    task_id: Annotated[str, Field(description="Task to move.")],
    to_list: Annotated[
        str | None,
        Field(description="Destination list on the same project, by name or id."),
    ] = None,
    to_project_id: Annotated[
        str | None,
        Field(description="Move to a different project (tab). Its todo stage is used "
                          "unless to_list names a list there."),
    ] = None,
) -> dict[str, Any]:
    """Move a task to another list, or to a different project entirely. Use this
    for stages that are not part of the todo/in_progress/review/done flow - a
    'Blocked' or 'Icebox' list, say - where update_status does not apply."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    denied = await require(view.board_id, AUTHOR, "move a task")
    if denied:
        return denied
    guard = _require_not_someone_elses(view, memberships, my_id)
    if guard:
        return guard

    target_view = view
    if to_project_id and to_project_id != view.board_id:
        denied = await require(to_project_id, AUTHOR, "move a task into that project")
        if denied:
            return denied
        target_view = await _view(to_project_id, fresh=True)

    if to_list:
        destination = target_view.find_list(to_list)
        if destination is None:
            return {"ok": False, "error": f"No list called '{to_list}' on the target project.",
                    "lists": [l.get("name") for l in target_view.lists.values()]}
    else:
        todo_list_id = target_view.list_of_status.get(TODO)
        if not todo_list_id:
            return {"ok": False, "error": "Name a destination list; the target project "
                                          "has no obvious todo stage."}
        destination = target_view.lists[todo_list_id]

    destination_id = str(destination["id"])
    position = next_position(
        [c.get("position") for c in target_view.cards_in_list(destination_id)]
    )
    fields: dict[str, Any] = {"listId": destination_id, "position": position}
    if target_view.board_id != view.board_id:
        fields["boardId"] = target_view.board_id

    await client.patch_card(task_id, fields)
    client.invalidate_board(view.board_id)
    client.invalidate_board(target_view.board_id)
    return {
        "ok": True,
        "result": "moved",
        "task_id": task_id,
        "to_project": target_view.board_name,
        "to_list": destination.get("name"),
        "status_now": target_view.status_of_list.get(destination_id) or "not a work stage",
        "note": "Planka marks cards in a closed list as finished."
        if (destination.get("type") == "closed")
        else None,
    }


@mcp.tool(annotations=WRITE)
@tool_result
async def create_board(
    name: Annotated[str, Field(description="Name of the new board (container).")],
    shared: Annotated[
        bool,
        Field(description="Shared boards accept several managers, which is what a "
                          "team wants. A private board has a single owner and cannot "
                          "take co-managers later without being converted."),
    ] = True,
    description: Annotated[
        str | None, Field(description="What this board is for.")
    ] = None,
) -> dict[str, Any]:
    """Create a board - the container that holds projects (tabs).

    Needs an `admin` or `projectOwner` account; Planka refuses project creation
    for anyone else. Create the board first, then fill it with `create_project`
    or `copy_project_structure`."""
    config, client = await _get_client()
    me = await client.me()
    if me.get("role") not in ("admin", "projectOwner"):
        return {
            "ok": False,
            "result": "not_permitted",
            "reason": f"Creating a board needs an admin or projectOwner account; "
                      f"this one is '{me.get('role')}'.",
            "next_step": "Ask an administrator to create it, or to raise this "
                         "account to projectOwner.",
        }

    container = await client.create_container(name.strip(), "shared" if shared else "private")
    if description:
        await client.update_container(str(container["id"]), {"description": description})
    return {
        "ok": True,
        "result": "created",
        "board_id": str(container.get("id")),
        "name": container.get("name"),
        "shared": shared,
        "next_step": "Add projects to it with create_project or copy_project_structure.",
    }


@mcp.tool(annotations=WRITE)
@tool_result
async def copy_project_structure(
    source_project_id: Annotated[
        str, Field(description="Project (tab) whose layout to copy.")
    ],
    name: Annotated[str, Field(description="Name for the new project.")],
    board_id: Annotated[
        str | None,
        Field(description="Board (container) to create it in. Defaults to the "
                          "source's own board."),
    ] = None,
    include_labels: Annotated[
        bool, Field(description="Recreate the source's labels. On by default.")
    ] = True,
    include_members: Annotated[
        bool,
        Field(description="Give the source's members the same roles on the copy. "
                          "Off by default, and needs membership rights."),
    ] = False,
) -> dict[str, Any]:
    """Clone a project's layout: its columns with their exact types and order, and
    optionally its labels and members. **No cards are copied** - this produces an
    empty project shaped like the original.

    Prefer this over `create_project` when an existing project is the template.
    Naming a column 'Done' is not the same as copying it: a list's type decides
    whether Planka marks cards dropped there as finished, and only a copy carries
    that across."""
    config, client = await _get_client()
    source = await _view(source_project_id, fresh=True)
    target_board = board_id or source.project_id
    if not target_board:
        return {"ok": False, "error": "Could not tell which board to create this in; "
                                      "pass board_id."}

    graph = await client.project_graph()
    managers = {
        str(m.get("userId")) for m in (graph.get("included") or {}).get("projectManagers") or []
        if str(m.get("projectId")) == target_board
    }
    me = await client.me()
    if me.get("role") != "admin" and str(me.get("id")) not in managers:
        return {"ok": False, "result": "not_permitted",
                "reason": "Creating a project requires managing the target board."}

    stages = [
        obj for obj in sorted(source.lists.values(), key=lambda l: l.get("position") or 0)
        if (obj.get("type") or "active") not in SYSTEM_LIST_TYPES and obj.get("name")
    ]
    if not stages:
        return {"ok": False, "result": "nothing_to_copy",
                "reason": f"'{source.board_name}' has no columns of its own to copy."}

    board = await client.create_board(
        target_board, name.strip(),
        next_position([
            b.get("position") for b in (graph.get("included") or {}).get("boards") or []
            if str(b.get("projectId")) == target_board
        ]),
    )
    new_id = str(board.get("id"))

    copied_lists = []
    for i, stage in enumerate(stages):
        created = await client.create_list(
            new_id, stage.get("name"), (i + 1) * 65536, stage.get("type") or "active"
        )
        copied_lists.append({"name": created.get("name"), "type": created.get("type")})

    copied_labels = []
    if include_labels:
        for i, label in enumerate(
            sorted(source.labels.values(), key=lambda l: l.get("position") or 0)
        ):
            created = await client.create_label(
                new_id, label.get("name") or "", label.get("color") or LABEL_COLORS[0],
                (i + 1) * 65536,
            )
            copied_labels.append(created.get("name"))

    copied_members, member_note = [], None
    if include_members:
        denied = await require(new_id, PEOPLE, "copy members onto the new project")
        if denied:
            member_note = ("Your role cannot manage membership, so the copy was made "
                           "without members.")
        else:
            my_id = str(me.get("id"))
            for membership in source.board_memberships:
                user_id = str(membership.get("userId"))
                if user_id == my_id:
                    continue  # the creator is already on it
                await client.add_board_membership(
                    new_id, user_id, str(membership.get("role") or "worker")
                )
                copied_members.append({
                    "name": (source.users.get(user_id) or {}).get("name") or user_id,
                    "role": membership.get("role"),
                })

    client.invalidate_board(new_id)
    result = {
        "ok": True,
        "result": "copied",
        "project_id": new_id,
        "name": board.get("name"),
        "copied_from": source.board_name,
        "lists": copied_lists,
        "labels": copied_labels,
        "members": copied_members,
        "cards_copied": 0,
        "note": "Structure only - no cards were copied.",
    }
    skipped = len(source.lists) - len(stages)
    if skipped:
        result["system_lists_skipped"] = skipped
    if member_note:
        result["members_note"] = member_note
    return result

@mcp.tool(annotations=WRITE)
@tool_result
async def update_board(
    board_id: Annotated[str, Field(description="Board (container) to change.")],
    name: Annotated[str | None, Field(description="New name.")] = None,
    description: Annotated[str | None, Field(description="What this board is for.")] = None,
    hidden: Annotated[
        bool | None, Field(description="Hide it from the boards list without deleting it.")
    ] = None,
    favorite: Annotated[
        bool | None, Field(description="Mark it a favourite for this account.")
    ] = None,
) -> dict[str, Any]:
    """Rename a board (container), describe it, hide it or favourite it. Requires
    managing that board - instance admin, or one of its managers."""
    config, client = await _get_client()
    graph = await client.project_graph()
    managers = {
        str(m.get("userId")) for m in (graph.get("included") or {}).get("projectManagers") or []
        if str(m.get("projectId")) == board_id
    }
    me = await client.me()
    if me.get("role") != "admin" and str(me.get("id")) not in managers:
        return {"ok": False, "result": "not_permitted",
                "reason": "Changing a board requires managing it."}

    fields: dict[str, Any] = {}
    if name:
        fields["name"] = name.strip()
    if description is not None:
        fields["description"] = description
    if hidden is not None:
        fields["isHidden"] = hidden
    if favorite is not None:
        fields["isFavorite"] = favorite
    if not fields:
        return {"ok": True, "result": "nothing_to_change", "board_id": board_id}

    updated = await client.update_container(board_id, fields)
    return {"ok": True, "result": "updated", "board_id": board_id,
            "name": updated.get("name"), "changed": sorted(fields)}
