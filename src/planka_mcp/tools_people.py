"""People: who is on a project, what role they hold, and who is on a task.

The flagship flow here is "put these two people on this task": resolve the
names, check they are members of the project, add them as workers if they are
not, then assign them to the task - one call, all of it enforced server-side.
"""

from __future__ import annotations

import asyncio
import re
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
from .domain import DONE, BoardView
from .roles import ASSIGN, BOARD_ROLES, INSTANCE_ROLES, PEOPLE

# Planka writes a mention as `@[Display Name](userId)`.
MENTION_RE = re.compile(r"@\[([^\]]*)\]\(([^)\s]+)\)")

# Phrasing that turns a mention into a hand-off rather than a passing reference.
HANDOFF_PHRASES = (
    "assign", "assigned to", "assigning", "over to", "handing", "hand over",
    "please", "can you", "could you", "take this", "take over", "your turn",
    "to review", "to check", "to do", "for you", "ping", "own this", "owns this",
    "responsible", "action", "follow up", "todo", "to-do",
)


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
        str,
        Field(description="set_instance_role | add_board_manager | create_api_key | "
                          "revoke_api_key | create_person."),
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
    a manager of a board, or mint and revoke API keys. Off unless the operator
    sets PLANKA_ALLOW_USER_ADMIN, and this server will never raise its own
    account's role or hand out `admin`.

    `create_api_key` returns the key **once** - Planka cannot show it again, and
    it replaces any key that account already had, breaking whatever was using it.
    A key is a long-lived credential that acts as that person, and the value will
    pass through this conversation and any transcript of it, so treat the output
    as a secret and hand it over out of band. `revoke_api_key` clears one."""
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

    if action in ("create_api_key", "revoke_api_key"):
        if resolved is None:
            return {"ok": False, "error": f"person is required for {action}."}
        target_id = str(resolved["id"])
        is_self = target_id == str(me.get("id"))
        had_key = bool(resolved.get("apiKeyPrefix"))

        if action == "revoke_api_key":
            if is_self and config.api_key:
                return {"ok": False, "result": "refused",
                        "reason": "That is the key this server is authenticating with; "
                                  "revoking it would cut off its own access."}
            await client.revoke_api_key(target_id)
            return {"ok": True, "result": "key_revoked", **_person_brief(resolved),
                    "had_key": had_key,
                    "note": "Anything using that key stops working immediately."}

        if is_self and config.api_key:
            return {"ok": False, "result": "refused",
                    "reason": "Minting a key for this account would replace the key "
                              "this server is authenticating with, cutting off its "
                              "own access mid-session.",
                    "next_step": "Mint it from an email/password session instead."}

        _, key = await client.create_api_key(target_id)
        if not key:
            return {"ok": False, "error": "Planka returned no key."}
        return {
            "ok": True,
            "result": "key_created",
            **_person_brief(resolved),
            "api_key": key,
            "replaced_an_existing_key": had_key,
            "warning": "Shown once and not retrievable again. This key acts as this "
                       "person until revoked, and it is now in this conversation and "
                       "any log of it - hand it over out of band and revoke it if the "
                       "transcript is not trusted."
            + (" It replaced their previous key, so anything using that has stopped "
               "working." if had_key else ""),
        }

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

    return {"ok": False, "error": "action must be set_instance_role, add_board_manager, "
                                  "create_api_key, revoke_api_key or create_person."}


@mcp.tool(annotations=READ_ONLY)
@tool_result
async def find_informal_assignments(
    project_id: Annotated[
        str | None, Field(description="Check one project (tab) instead of all of them.")
    ] = None,
    board_id: Annotated[
        str | None, Field(description="Check every project inside one board (container).")
    ] = None,
    include_any_mention: Annotated[
        bool,
        Field(description="Also report plain mentions with no hand-off wording. Off by "
                          "default, which keeps this to comments that actually read like "
                          "an assignment."),
    ] = False,
    include_done: Annotated[
        bool, Field(description="Include finished tasks. Off by default.")
    ] = False,
    limit: Annotated[int, Field(description="Maximum findings (1-100).", ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Find work handed out in comments but never actually assigned.

    Teams often hand off by writing "Assigned to: @[Name](id)" in a comment. In
    Planka that is just text: the person is not a member of the task, so it never
    reaches their queue, no board view shows them as owner, and it is invisible to
    reporting. This reports every such comment where the mentioned person is not a
    member of the task, and gives you the exact `assign_people` call that would
    make it real. Read-only - it changes nothing on its own. Confirm with a human
    before acting on findings; a mention is not always a hand-off.
    """
    config, client = await _get_client()

    if project_id:
        if not config.board_allowed(project_id):
            return {"ok": False, "error": "That project is outside this server's allowlist."}
        board_ids = [project_id]
    elif board_id:
        graph = await client.project_graph()
        board_ids = [
            str(b["id"]) for b in (graph.get("included") or {}).get("boards") or []
            if str(b.get("projectId")) == board_id and config.board_allowed(str(b["id"]))
        ]
    else:
        board_ids = [b for b in await client.board_ids() if config.board_allowed(b)]
    if not board_ids:
        return {"ok": True, "count": 0, "total_findings": 0, "scanned_projects": 0,
                "findings": [], "note": "No projects in scope."}

    payloads = await asyncio.gather(
        *(client.board(b) for b in board_ids), return_exceptions=True
    )
    views = [
        BoardView(p, config.status_lists) for p in payloads if not isinstance(p, BaseException)
    ]

    targets = [
        (view, card_id)
        for view in views
        for card_id, card in view.cards.items()
        if card.get("commentsTotal")
        and (include_done or not (card.get("isClosed") or view.status_of_card(card) == DONE))
    ]
    if not targets:
        return {"ok": True, "count": 0, "total_findings": 0,
                "scanned_projects": len(views), "findings": [],
                "note": "No commented tasks in scope."}

    async def comments_for(card_id: str) -> list[dict[str, Any]]:
        try:
            return await client.comments(card_id)
        except PlankaError:
            return []

    fetched = await asyncio.gather(*(comments_for(cid) for _, cid in targets))

    findings: list[dict[str, Any]] = []
    for (view, card_id), comments in zip(targets, fetched):
        card = view.cards[card_id]
        members = set(view.members_of_card.get(card_id, []))
        seen: set[str] = set()
        for comment in comments:
            text = comment.get("text") or ""
            mentioned = MENTION_RE.findall(text)
            if not mentioned:
                continue
            lowered = text.lower()
            reads_like_handoff = any(phrase in lowered for phrase in HANDOFF_PHRASES)
            if not reads_like_handoff and not include_any_mention:
                continue
            for name, user_id in mentioned:
                if user_id in members or user_id in seen:
                    continue
                seen.add(user_id)
                findings.append({
                    "task_id": card_id,
                    "title": card.get("name"),
                    "project": view.board_name,
                    "project_id": view.board_id,
                    "status": view.status_of_card(card) or "not a work stage",
                    "person": {"user_id": user_id,
                               "name": (view.users.get(user_id) or {}).get("name") or name},
                    "said_by": (view.users.get(str(comment.get("userId"))) or {}).get("name"),
                    "said_at": comment.get("createdAt"),
                    "comment": text[:300],
                    "reads_like_handoff": reads_like_handoff,
                    "currently_assigned_to": [
                        (view.users.get(m) or {}).get("name") or m for m in members
                    ],
                    "url": f"{config.base_url.rstrip('/')}/cards/{card_id}",
                    "to_make_it_real": {
                        "tool": "assign_people",
                        "arguments": {"task_id": card_id, "people": [user_id]},
                    },
                })

    findings.sort(key=lambda f: (not f["reads_like_handoff"], str(f["said_at"] or "")))
    result: dict[str, Any] = {
        "ok": True,
        "count": min(len(findings), limit),
        "total_findings": len(findings),
        "scanned_projects": len(views),
        "findings": findings[:limit],
        "why_this_matters": "A mention is plain text. Until someone is a member of "
                            "the task it stays out of their queue and out of any "
                            "report of who is doing what.",
        "next_step": "Check with a human before assigning - a mention can be a "
                     "question rather than a hand-off. Then call assign_people.",
    }
    if len(findings) > limit:
        result["note"] = f"Showing {limit} of {len(findings)}; narrow the scope or raise limit."
    return result
