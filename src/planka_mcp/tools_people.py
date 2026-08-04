"""People: who is on a project, what role they hold, and who is on a task.

The flagship flow here is "put these two people on this task": resolve the
names, check they are members of the project, add them as workers if they are
not, then assign them to the task - one call, all of it enforced server-side.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .app import (
    READ_ONLY,
    WRITE,
    _get_client,
    _load_card,
    effective_role,
    mcp,
    require,
    tool_result,
)
from .client import PlankaError
from .domain import BoardView
from .roles import ASSIGN, BOARD_ROLES, INSTANCE_ROLES, PEOPLE


async def _people_directory(client: Any, view: BoardView) -> list[dict[str, Any]]:
    """Everyone we can resolve names against: project members first, then the
    instance directory when the account is allowed to read it."""
    people = list(view.users.values())
    known = {str(p.get("id")) for p in people}
    try:
        for user in await client.users():
            if str(user.get("id")) not in known:
                people.append(user)
    except PlankaError:
        pass  # not an admin - project members are all we can see, which is fine
    return people


def _match_person(query: str, people: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve a name, an email or a raw id to exactly one person."""
    wanted = query.strip().lower()
    exact = [
        p for p in people
        if wanted in (str(p.get("id")), (p.get("email") or "").lower(),
                      (p.get("username") or "").lower(), (p.get("name") or "").lower())
    ]
    if len(exact) == 1:
        return exact[0], []
    partial = [
        p for p in people
        if wanted and (wanted in (p.get("name") or "").lower()
                       or wanted in (p.get("email") or "").lower())
    ]
    if len(partial) == 1:
        return partial[0], []
    return None, exact or partial


def _person_brief(person: dict[str, Any]) -> dict[str, Any]:
    return {"user_id": str(person.get("id")), "name": person.get("name"),
            "email": person.get("email")}


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def whoami() -> dict[str, Any]:
    """Who this server is acting as, and what it is allowed to do. Call it when a
    request might exceed your rights, so you can say so instead of failing."""
    config, client = await _get_client()
    me = await client.me()
    result = {
        "ok": True,
        "name": me.get("name"),
        "email": me.get("email"),
        "instance_role": me.get("role"),
        "configured_ceiling": config.act_as or "none (use the account's own rights)",
        "user_administration_enabled": config.allow_user_admin,
        "note": "Roles are per project; call list_projects or get_project for the "
                "role you hold on a specific one.",
    }
    if config.board_ids:
        result["restricted_to_projects"] = config.board_ids
    return result


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def list_people(
    project_id: Annotated[
        str | None,
        Field(description="Members of this project (tab) and their roles. Omit for "
                          "everyone on the instance."),
    ] = None,
) -> dict[str, Any]:
    """List people and their roles - members of one project, or the whole
    directory. Use it to resolve a partial name before assigning work."""
    config, client = await _get_client()
    if project_id:
        view = BoardView(await client.board(project_id, fresh=True), config.status_lists)
        structure = view.structure()
        return {"ok": True, "project": view.board_name, "members": structure["members"],
                "roles_available": list(BOARD_ROLES)}
    try:
        people = await client.users()
    except PlankaError as exc:
        return {"ok": False, "error": f"Cannot read the user directory: {exc}",
                "next_step": "Pass project_id to list that project's members instead."}
    return {"ok": True, "count": len(people),
            "people": [
                {**_person_brief(p), "instance_role": p.get("role"),
                 "deactivated": bool(p.get("isDeactivated"))}
                for p in people
            ]}


@mcp.tool(annotations=WRITE)
@tool_result
async def assign_people(
    task_id: Annotated[str, Field(description="Task to put people on.")],
    people: Annotated[
        list[str],
        Field(description="Names, emails or ids, e.g. ['Ada', 'grace@example.com']."),
    ],
    add_to_project_as: Annotated[
        str,
        Field(description="Board role to grant if someone is not yet a member of the "
                          "project: worker (default), editor or guest."),
    ] = "worker",
) -> dict[str, Any]:
    """Assign people to a task, adding them to the project first if they are not
    members yet. Each name is resolved against the project's members and the user
    directory; an ambiguous name is reported rather than guessed. Granting project
    membership needs membership rights, so a worker can assign existing members
    but cannot pull new people onto the project."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    denied = await require(view.board_id, ASSIGN, "assign other people to a task")
    if denied:
        return denied
    if add_to_project_as not in BOARD_ROLES:
        return {"ok": False, "error": f"add_to_project_as must be one of {list(BOARD_ROLES)}."}

    directory = await _people_directory(client, view)
    already = set(view.members_of_card.get(task_id, []))
    assigned, added_to_project, unresolved, skipped = [], [], [], []

    can_add_members = (await effective_role(view.board_id)).can(PEOPLE)

    for query in people:
        person, candidates = _match_person(query, directory)
        if person is None:
            unresolved.append({
                "asked_for": query,
                "why": "no match" if not candidates else "more than one match",
                "candidates": [_person_brief(c) for c in candidates][:5],
            })
            continue
        user_id = str(person.get("id"))

        if view.membership_of(user_id) is None:
            if not can_add_members:
                skipped.append({**_person_brief(person),
                                "why": "not a member of this project, and your role "
                                       "cannot add members"})
                continue
            status, _ = await client.add_board_membership(
                view.board_id, user_id, add_to_project_as
            )
            added_to_project.append({**_person_brief(person), "role": add_to_project_as,
                                     "already_member": status == 409})

        if user_id in already:
            assigned.append({**_person_brief(person), "already_assigned": True})
            continue
        await client.add_membership(task_id, user_id)
        assigned.append({**_person_brief(person), "already_assigned": False})

    client.invalidate_board(view.board_id)
    landed = bool(assigned or added_to_project)
    result = {
        # nothing assigned and something unresolved is a failure, not a success
        "ok": landed or not (unresolved or skipped),
        "result": "assigned" if landed else "nobody_assigned",
        "task_id": task_id,
        "title": card.get("name"),
        "assigned": assigned,
        "added_to_project": added_to_project,
    }
    if skipped:
        result["skipped"] = skipped
    if unresolved:
        result["unresolved"] = unresolved
        result["next_step"] = "Call list_people to see exact names, then retry."
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def unassign_people(
    task_id: Annotated[str, Field(description="Task to take people off.")],
    people: Annotated[list[str], Field(description="Names, emails or ids to unassign.")],
) -> dict[str, Any]:
    """Take people off a task. Their project membership is untouched - this only
    removes the assignment."""
    config, client, card, view, memberships, my_id = await _load_card(task_id)
    denied = await require(view.board_id, ASSIGN, "unassign other people")
    if denied:
        return denied

    directory = await _people_directory(client, view)
    current = set(view.members_of_card.get(task_id, []))
    removed, unresolved, untouched = [], [], []
    for query in people:
        person, candidates = _match_person(query, directory)
        if person is None:
            unresolved.append({"asked_for": query,
                               "candidates": [_person_brief(c) for c in candidates][:5]})
            continue
        user_id = str(person.get("id"))
        if user_id not in current:
            untouched.append({**_person_brief(person), "why": "was not assigned"})
            continue
        await client.remove_membership(task_id, user_id)
        removed.append(_person_brief(person))

    client.invalidate_board(view.board_id)
    result = {"ok": bool(removed) or not unresolved,
              "result": "unassigned" if removed else "nobody_unassigned",
              "task_id": task_id, "removed": removed}
    if untouched:
        result["untouched"] = untouched
    if unresolved:
        result["unresolved"] = unresolved
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def set_project_members(
    project_id: Annotated[str, Field(description="Project (tab) to change membership on.")],
    people: Annotated[list[str], Field(description="Names, emails or ids.")],
    role: Annotated[
        str,
        Field(description="worker (works on assigned cards) | editor (full board "
                          "editing) | guest (read, comment) | none (remove them)."),
    ] = "worker",
) -> dict[str, Any]:
    """Add people to a project, change the role they hold, or remove them from it.
    Requires membership rights on that project. Removing a member does not delete
    anything they created, and never touches their account."""
    config, client = await _get_client()
    if not config.board_allowed(project_id):
        return {"ok": False, "error": "That project is outside this server's allowlist."}
    denied = await require(project_id, PEOPLE, "change who is on this project")
    if denied:
        return denied
    if role not in (*BOARD_ROLES, "none"):
        return {"ok": False, "error": f"role must be one of {[*BOARD_ROLES, 'none']}."}

    view = BoardView(await client.board(project_id, fresh=True), config.status_lists)
    directory = await _people_directory(client, view)
    me = await client.me()

    added, changed, removed, unresolved, refused = [], [], [], [], []
    for query in people:
        person, candidates = _match_person(query, directory)
        if person is None:
            unresolved.append({"asked_for": query,
                               "candidates": [_person_brief(c) for c in candidates][:5]})
            continue
        user_id = str(person.get("id"))
        membership = view.membership_of(user_id)

        if role == "none":
            if membership is None:
                refused.append({**_person_brief(person), "why": "not a member"})
                continue
            if user_id == str(me.get("id")):
                refused.append({**_person_brief(person),
                                "why": "this server will not remove its own access"})
                continue
            await client.remove_board_membership(str(membership["id"]))
            removed.append(_person_brief(person))
            continue

        if membership is None:
            await client.add_board_membership(project_id, user_id, role)
            added.append({**_person_brief(person), "role": role})
        elif membership.get("role") != role:
            previous = membership.get("role")  # read before the write, not after
            await client.update_board_membership(str(membership["id"]), {"role": role})
            changed.append({**_person_brief(person), "from": previous, "to": role})
        else:
            refused.append({**_person_brief(person), "why": f"already {role}"})

    client.invalidate_board(project_id)
    result = {"ok": True, "result": "membership_updated", "project": view.board_name,
              "added": added, "role_changed": changed, "removed": removed}
    if refused:
        result["no_change"] = refused
    if unresolved:
        result["unresolved"] = unresolved
    return result


@mcp.tool(annotations=WRITE)
@tool_result
async def admin_manage_person(
    action: Annotated[
        str, Field(description="set_instance_role | create_person | add_board_manager.")
    ],
    person: Annotated[
        str | None,
        Field(description="Existing person (name, email or id) for set_instance_role "
                          "and add_board_manager."),
    ] = None,
    instance_role: Annotated[
        str | None,
        Field(description="admin | projectOwner | boardUser | guestOnly."),
    ] = None,
    board_id: Annotated[
        str | None, Field(description="Board (container) id for add_board_manager.")
    ] = None,
    email: Annotated[str | None, Field(description="Email for create_person.")] = None,
    name: Annotated[str | None, Field(description="Display name for create_person.")] = None,
) -> dict[str, Any]:
    """Instance-level administration: change someone's instance role, make someone
    a manager of a board, or create a person. Off unless the operator sets
    PLANKA_ALLOW_USER_ADMIN, and this server will never raise its own account's
    role or hand out `admin`."""
    config, client = await _get_client()
    if not config.allow_user_admin:
        return {
            "ok": False,
            "result": "not_permitted",
            "reason": "User administration is disabled on this server.",
            "next_step": "The operator must set PLANKA_ALLOW_USER_ADMIN=true.",
        }
    me = await client.me()
    if me.get("role") != "admin":
        return {"ok": False, "result": "not_permitted",
                "reason": f"This account is '{me.get('role')}'; instance administration "
                          "requires admin."}

    directory = await client.users()
    resolved: dict[str, Any] | None = None
    if person:
        resolved, candidates = _match_person(person, directory)
        if resolved is None:
            return {"ok": False, "result": "unresolved", "asked_for": person,
                    "candidates": [_person_brief(c) for c in candidates][:5]}

    if action == "set_instance_role":
        if instance_role not in INSTANCE_ROLES:
            return {"ok": False, "error": f"instance_role must be one of {list(INSTANCE_ROLES)}."}
        if resolved is None:
            return {"ok": False, "error": "person is required for set_instance_role."}
        if str(resolved.get("id")) == str(me.get("id")):
            return {"ok": False, "result": "refused",
                    "reason": "This server will not change its own account's role."}
        if instance_role == "admin":
            return {"ok": False, "result": "refused",
                    "reason": "Granting instance admin is left to a human.",
                    "next_step": "Do it in the Planka admin panel."}
        updated = await client.set_instance_role(str(resolved["id"]), instance_role)
        return {"ok": True, "result": "role_changed", **_person_brief(resolved),
                "instance_role": updated.get("role")}

    if action == "add_board_manager":
        if resolved is None or not board_id:
            return {"ok": False, "error": "person and board_id are required."}
        status, _ = await client.add_project_manager(board_id, str(resolved["id"]))
        return {"ok": True, "result": "manager_added" if status != 409 else "already_manager",
                **_person_brief(resolved), "board_id": board_id}

    if action == "create_person":
        if not email or not name:
            return {"ok": False, "error": "email and name are required for create_person."}
        return {
            "ok": False,
            "result": "refused",
            "reason": "Creating an account needs a password, which this server will "
                      "not set or transmit.",
            "next_step": "Create the person in the Planka admin panel, then use "
                         "set_project_members to put them on a project.",
        }

    return {"ok": False, "error": "action must be set_instance_role, "
                                  "add_board_manager or create_person."}
