"""Offline tests for board management: roles, people, structure."""

from __future__ import annotations

from fake_planka import (
    BOARD,
    CONTAINER,
    GRACE,
    LIST_BLOCKED,
    LIST_DONE,
    LIST_EMPTY,
    LIST_TODO,
    ME,
    OTHER,
    ADA,
    run,
    setup_fake,
)

from planka_mcp import server as S


# ---------------------------------------------------------------- roles


def test_effective_role_folds_both_axes():
    from planka_mcp.app import effective_role

    fake = setup_fake()
    assert run(effective_role(BOARD)).role == "editor"  # board membership

    fake.board_memberships[0]["role"] = "worker"
    assert run(effective_role(BOARD)).role == "worker"

    fake.project_managers.append({"projectId": CONTAINER, "userId": ME})
    assert run(effective_role(BOARD)).role == "admin"  # manager of the container

    fake.project_managers.clear()
    fake.instance_role = "admin"
    assert run(effective_role(BOARD)).role == "admin"  # instance admin

    fake.instance_role = "boardUser"
    fake.board_memberships[:] = [m for m in fake.board_memberships if m["userId"] != ME]
    assert run(effective_role(BOARD)).role == "guest"  # no membership at all


def test_act_as_caps_the_account_but_never_raises_it():
    from planka_mcp.app import effective_role

    fake = setup_fake(act_as="worker")
    fake.instance_role = "admin"
    role = run(effective_role(BOARD))
    assert role.role == "worker" and role.account_role == "admin"
    assert role.capped_to == "worker"

    setup_fake(act_as="admin")  # membership is editor; the cap cannot promote it
    assert run(effective_role(BOARD)).role == "editor"


def test_a_worker_cannot_reshape_the_project_or_assign_others():
    fake = setup_fake()
    fake.board_memberships[0]["role"] = "worker"

    for call in (
        S.create_list(BOARD, "New stage"),
        S.manage_labels(BOARD, create=["priority: urgent"]),
        S.assign_people("c1", ["Ada"]),
        S.set_project_members(BOARD, ["Ada"], role="editor"),
        S.update_project(BOARD, name="Renamed"),
    ):
        result = run(call)
        assert result["result"] == "not_permitted", result
        assert "worker" in result["reason"]
    assert not any(c.startswith("add_member") for c in fake.calls)


def test_an_editor_may_shape_the_project_but_not_membership():
    setup_fake()  # membership role editor
    assert run(S.create_list(BOARD, "Ready for QA"))["ok"] is True
    assert run(S.manage_labels(BOARD, create=["priority: urgent"]))["ok"] is True
    assert run(S.set_project_members(BOARD, ["Ada"], role="worker"))["result"] == "not_permitted"


def test_whoami_reports_the_ceiling():
    setup_fake(act_as="editor")
    result = run(S.whoami())
    assert result["ok"] is True
    assert result["configured_ceiling"] == "editor"
    assert result["user_administration_enabled"] is False


# ---------------------------------------------------------------- people


def test_assign_people_adds_them_to_the_project_as_workers_first():
    fake = setup_fake()
    fake.instance_role = "admin"  # so the directory is readable and PEOPLE is granted

    result = run(S.assign_people("c1", ["Ada", "grace@example.test"]))
    assert result["ok"] is True
    assert {p["name"] for p in result["assigned"]} == {"Ada Hopper", "Grace Hopper"}
    assert {p["role"] for p in result["added_to_project"]} == {"worker"}
    assert {m["userId"] for m in fake.board_memberships} >= {ADA, GRACE}
    assert {m["userId"] for m in fake.memberships if m["cardId"] == "c1"} == {ADA, GRACE}


def test_assign_people_reports_ambiguity_instead_of_guessing():
    fake = setup_fake()
    fake.instance_role = "admin"
    result = run(S.assign_people("c1", ["Hopper"]))  # matches nobody uniquely once both share a surname
    assert result["ok"] is False and result["result"] == "nobody_assigned"
    assert result["unresolved"][0]["why"] == "more than one match"
    assert len(result["unresolved"][0]["candidates"]) == 2
    assert result["assigned"] == []


def test_assign_people_is_idempotent_and_skips_non_members_for_a_low_role():
    fake = setup_fake()
    fake.instance_role = "admin"
    run(S.assign_people("c1", ["Ada"]))
    again = run(S.assign_people("c1", ["Ada"]))
    assert again["assigned"][0]["already_assigned"] is True
    assert len([m for m in fake.memberships if m["cardId"] == "c1"]) == 1

    # an editor can assign existing members but cannot pull new people in
    fake2 = setup_fake()
    result = run(S.assign_people("c1", ["Ada"]))
    assert result["skipped"][0]["name"] == "Ada Hopper"
    assert not any(m["userId"] == ADA for m in fake2.board_memberships)


def test_unassign_leaves_project_membership_intact():
    fake = setup_fake()
    fake.instance_role = "admin"
    run(S.assign_people("c1", ["Ada"]))
    result = run(S.unassign_people("c1", ["Ada"]))
    assert result["removed"][0]["user_id"] == ADA
    assert not any(m["cardId"] == "c1" and m["userId"] == ADA for m in fake.memberships)
    assert any(m["userId"] == ADA for m in fake.board_memberships)  # still on the project


def test_set_project_members_changes_roles_and_refuses_self_removal():
    fake = setup_fake()
    fake.instance_role = "admin"

    added = run(S.set_project_members(BOARD, ["Ada"], role="worker"))
    assert added["added"][0]["role"] == "worker"

    promoted = run(S.set_project_members(BOARD, ["Ada"], role="editor"))
    assert promoted["role_changed"][0] == {"user_id": ADA, "name": "Ada Hopper",
                                           "email": "ada@example.test",
                                           "from": "worker", "to": "editor"}

    removed = run(S.set_project_members(BOARD, ["Ada"], role="none"))
    assert removed["removed"][0]["user_id"] == ADA
    assert not any(m["userId"] == ADA for m in fake.board_memberships)

    myself = run(S.set_project_members(BOARD, ["agent@example.test"], role="none"))
    assert "will not remove its own access" in myself["no_change"][0]["why"]
    assert any(m["userId"] == ME for m in fake.board_memberships)


def test_user_administration_is_off_by_default_and_never_grants_admin():
    setup_fake()
    off = run(S.admin_manage_person("set_instance_role", person="Ada",
                                    instance_role="projectOwner"))
    assert off["result"] == "not_permitted"

    fake = setup_fake(allow_user_admin=True)
    fake.instance_role = "admin"
    promoted = run(S.admin_manage_person("set_instance_role", person="Ada",
                                         instance_role="projectOwner"))
    assert promoted["instance_role"] == "projectOwner"

    escalation = run(S.admin_manage_person("set_instance_role", person="Ada",
                                           instance_role="admin"))
    assert escalation["result"] == "refused"

    itself = run(S.admin_manage_person("set_instance_role", person="agent@example.test",
                                       instance_role="boardUser"))
    assert itself["result"] == "refused"

    creating = run(S.admin_manage_person("create_person", email="x@y.z", name="X"))
    assert creating["result"] == "refused"  # passwords are never handled here


# ---------------------------------------------------------------- structure


def test_delete_list_refuses_anything_holding_work():
    fake = setup_fake()
    busy = run(S.delete_list(BOARD, "To Do"))
    assert busy["result"] == "not_empty" and "move" in busy["next_step"].lower()
    assert fake.deleted == []

    empty = run(S.delete_list(BOARD, "Icebox"))
    assert empty["ok"] is True and fake.deleted == [f"list:{LIST_EMPTY}"]


def test_retiring_a_stage_keeps_its_cards():
    fake = setup_fake()
    result = run(S.update_list(BOARD, "To Do", list_type="inactive"))
    assert result["ok"] is True and result["type"] == "inactive"
    assert fake.cards["c1"]["listId"] == LIST_TODO  # nothing moved, nothing lost


def test_closing_a_list_warns_about_planka_finishing_its_cards():
    setup_fake()
    result = run(S.update_list(BOARD, "To Do", list_type="closed"))
    assert "closed list as finished" in result["warning"]


def test_labels_in_use_are_never_deleted():
    fake = setup_fake()
    fake.card_labels.append({"cardId": "c1", "labelId": "lb1"})
    result = run(S.manage_labels(BOARD, delete_unused=["priority: high", "size: M"]))
    assert result["deleted"] == ["size: M"]
    assert result["refused"][0]["why"] == "still on 1 card(s)"


def test_set_task_labels_adds_and_removes_the_link_only():
    fake = setup_fake()
    run(S.set_task_labels("c1", add=["priority: high"]))
    assert any(cl["cardId"] == "c1" for cl in fake.card_labels)
    run(S.set_task_labels("c1", remove=["priority: high"]))
    assert not any(cl["cardId"] == "c1" for cl in fake.card_labels)
    assert any(l["name"] == "priority: high" for l in fake.labels)  # label itself survives

    unknown = run(S.set_task_labels("c1", add=["nope"]))
    assert unknown["labels_not_found"] == ["nope"]


def test_move_task_reaches_lists_outside_the_status_flow():
    fake = setup_fake()
    result = run(S.move_task("c1", to_list="Blocked"))
    assert result["ok"] is True
    assert fake.cards["c1"]["listId"] == LIST_BLOCKED
    assert result["status_now"] == "not a work stage"


def test_move_task_refuses_someone_elses_work():
    fake = setup_fake()
    result = run(S.move_task("c2", to_list="Blocked"))
    assert result["result"] == "not_yours"
    assert fake.cards["c2"]["listId"] == LIST_TODO


def test_get_project_reports_structure_and_your_role():
    setup_fake()
    result = run(S.get_project(BOARD))
    project = result["project"]
    assert project["your_role"]["role"] == "editor"
    assert {l["name"] for l in project["lists"]} >= {"To Do", "Blocked", "Icebox"}
    blocked = next(l for l in project["lists"] if l["name"] == "Blocked")
    assert blocked["maps_to_status"] == "not a work stage"
    assert {m["role"] for m in project["members"]} == {"editor", "worker"}


def test_system_lists_are_never_treated_as_work_stages():
    from planka_mcp.domain import classify_list

    for system_type in ("inbox", "recurring", "archive", "trash"):
        # even carrying a name that matches a stage heuristic
        assert classify_list({"id": "x", "name": "To Do", "type": system_type}, {}, BOARD) is None
    assert classify_list({"id": "x", "name": "To Do", "type": "active"}, {}, BOARD) == "todo"
    assert classify_list({"id": "x", "name": "Anything", "type": "closed"}, {}, BOARD) == "done"
    assert classify_list({"id": "x", "name": "On hold", "type": "waiting"}, {}, BOARD) is None
