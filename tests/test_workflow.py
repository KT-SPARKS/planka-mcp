"""Offline tests for the work-queue guardrails: claiming, transitions, ownership."""

from __future__ import annotations

from fake_planka import (  # noqa: F401
    BOARD,
    GRACE,
    LIST_DOING,
    LIST_DONE,
    LIST_TODO,
    ME,
    OTHER,
    ADA,
    FakePlanka,
    run,
    setup_fake,
)

from planka_mcp import server as S  # noqa: E402


# --------------------------------------------------------------------------


def test_only_ready_work_is_listed():
    setup_fake()
    result = run(S.list_actionable_tasks())
    assert result["ok"] is True
    ids = [t["task_id"] for t in result["tasks"]]
    assert ids == ["c1"]  # c2 is someone else's, c3 is done
    assert result["filtered_out"]["assigned to someone else"] == 1
    assert result["filtered_out"]["not in a todo stage"] == 1


def test_claim_succeeds_then_is_idempotent():
    fake = setup_fake()
    first = run(S.claim_task("c1"))
    assert first["ok"] is True and first["result"] == "claimed"
    second = run(S.claim_task("c1"))
    assert second["ok"] is True and second["result"] == "already_yours"
    assert sum(1 for m in fake.memberships if m["cardId"] == "c1") == 1


def test_claim_refuses_task_held_by_someone_else():
    fake = setup_fake()
    result = run(S.claim_task("c2"))
    assert result["ok"] is False and result["result"] == "already_taken"
    assert result["held_by"] == ["Human"]
    assert not any(c.startswith("add_membership:c2") for c in fake.calls)


def test_claim_yields_when_a_rival_wins_the_race():
    fake = setup_fake()
    fake.races["c1"] = OTHER  # rival row appears with an earlier timestamp
    result = run(S.claim_task("c1"))
    assert result["ok"] is False and result["result"] == "already_taken"
    # our own row is rolled back, theirs is untouched
    assert not any(m["cardId"] == "c1" and m["userId"] == ME for m in fake.memberships)
    assert any(m["cardId"] == "c1" and m["userId"] == OTHER for m in fake.memberships)


def test_claim_rejects_finished_work():
    setup_fake()
    result = run(S.claim_task("c3"))
    assert result["ok"] is False and result["result"] == "not_claimable"


def test_status_change_requires_holding_the_claim():
    setup_fake()
    result = run(S.update_status("c1", "in_progress"))
    assert result["ok"] is False and result["result"] == "not_yours"


def test_illegal_transition_is_rejected():
    setup_fake()
    run(S.claim_task("c1"))
    result = run(S.update_status("c1", "done"))  # todo -> done is not legal
    assert result["ok"] is False and result["result"] == "illegal_transition"
    assert result["allowed_next_status"] == ["in_progress"]


def test_legal_transition_moves_the_card():
    fake = setup_fake()
    run(S.claim_task("c1"))
    result = run(S.update_status("c1", "in_progress"))
    assert result["ok"] is True and result["to"] == "in_progress"
    assert fake.cards["c1"]["listId"] == LIST_DOING
    again = run(S.update_status("c1", "in_progress"))
    assert again["result"] == "already_in_status"
    done = run(S.update_status("c1", "done"))
    assert done["ok"] is True and fake.cards["c1"]["listId"] == LIST_DONE


def test_done_is_terminal_unless_reopen_is_enabled():
    setup_fake()
    run(S.claim_task("c1"))
    run(S.update_status("c1", "in_progress"))
    run(S.update_status("c1", "done"))
    blocked = run(S.update_status("c1", "in_progress"))
    assert blocked["result"] == "illegal_transition"

    fake = setup_fake(allow_reopen=True)
    run(S.claim_task("c1"))
    run(S.update_status("c1", "in_progress"))
    run(S.update_status("c1", "done"))
    reopened = run(S.update_status("c1", "in_progress"))
    assert reopened["ok"] is True and fake.cards["c1"]["listId"] == LIST_DOING


def test_unknown_status_is_rejected():
    setup_fake()
    run(S.claim_task("c1"))
    result = run(S.update_status("c1", "shipped-ish"))
    assert result["ok"] is False and "Unknown status" in result["error"]


def test_add_result_requires_claim_and_is_idempotent():
    fake = setup_fake()
    denied = run(S.add_result("c1", "output"))
    assert denied["result"] == "not_yours"

    run(S.claim_task("c1"))
    first = run(S.add_result("c1", "output", idempotency_key="k1"))
    assert first["result"] == "recorded"
    retry = run(S.add_result("c1", "output", idempotency_key="k1"))
    assert retry["result"] == "already_recorded"
    assert len(fake.comment_store["c1"]) == 1


def test_release_only_removes_own_claim_and_returns_card_to_todo():
    fake = setup_fake()
    run(S.claim_task("c1"))
    run(S.update_status("c1", "in_progress"))
    result = run(S.release_task("c1", reason="out of scope"))
    assert result["ok"] is True and result["moved_to"] == "todo"
    assert fake.cards["c1"]["listId"] == LIST_TODO
    assert not any(m["cardId"] == "c1" for m in fake.memberships)

    foreign = run(S.release_task("c2"))
    assert foreign["result"] == "not_yours"
    assert any(m["cardId"] == "c2" and m["userId"] == OTHER for m in fake.memberships)


def test_board_allowlist_blocks_foreign_boards():
    setup_fake(board_ids=["other-board"])
    result = run(S.claim_task("c1"))
    assert result["ok"] is False and "not allowed" in result["error"]


def test_priority_label_is_never_read_as_a_size():
    from planka_mcp.domain import derive_effort
    assert derive_effort(["priority: medium", "size: L"], 0) == "l"
    assert derive_effort(["priority: high"], 0) is None
    assert derive_effort(["sp: 5"], 0) == "5 points"
    assert derive_effort([], 3) == "3 subtasks"


def test_dependency_links_are_not_counted_as_subtasks():
    from planka_mcp.domain import BoardView
    payload = {
        "item": {"id": BOARD, "name": "b"},
        "included": {
            "lists": [{"id": LIST_TODO, "name": "To Do", "type": "active", "position": 1}],
            "cards": [{"id": "c1", "boardId": BOARD, "listId": LIST_TODO, "name": "x",
                       "position": 1, "isClosed": False}],
            "cardMemberships": [], "cardLabels": [], "labels": [], "users": [],
            "taskLists": [{"id": "tl1", "cardId": "c1", "name": "Depends on"}],
            "tasks": [
                {"id": "t1", "taskListId": "tl1", "name": "real subtask", "isCompleted": False},
                {"id": "t2", "taskListId": "tl1", "linkedCardId": "c9", "isCompleted": False},
            ],
        },
    }
    view = BoardView(payload, {})
    summary = view.summarize(view.cards["c1"], ME)
    assert summary["subtasks"] == {"total": 1, "done": 0}


def test_library_boards_are_not_scanned_for_work():
    import asyncio as _asyncio
    from planka_mcp.client import PlankaClient
    from planka_mcp.config import Config

    payload = {
        "items": [{"id": "p1", "name": "P"}],
        "included": {
            "boards": [
                {"id": "b-work", "type": "project"},
                {"id": "b-docs", "type": "library"},
                {"id": "b-refs", "type": "collection"},
                {"id": "b-old"},  # no type -> treated as a work board
            ]
        },
    }

    def ids_for(**kw):
        client = PlankaClient(Config(base_url="https://x.test", api_key="k", **kw))
        client.projects = lambda: _make_future(payload)  # type: ignore[assignment]
        return _asyncio.run(client.board_ids())

    assert ids_for() == ["b-work", "b-old"]
    assert ids_for(board_types=[]) == ["b-work", "b-docs", "b-refs", "b-old"]
    assert ids_for(board_ids=["b-docs"]) == ["b-docs"]  # explicit allowlist wins


async def _make_future(value):
    return value


def test_create_task_lands_unclaimed_in_todo_with_known_labels_only():
    fake = setup_fake()
    result = run(S.create_task(
        "Ship the thing", description="details", board_id=BOARD,
        labels=["priority: high", "nonexistent"], checklist=["step one", "step two"],
    ))
    assert result["ok"] is True and result["status"] == "todo"
    assert result["labels_applied"] == ["priority: high"]
    assert result["labels_not_found"] == ["nonexistent"]
    assert "priority: high" in result["labels_available"]
    new_id = result["task_id"]
    assert fake.cards[new_id]["listId"] == LIST_TODO
    assert not any(m["cardId"] == new_id for m in fake.memberships)  # unclaimed
    assert [t["name"] for t in fake.tasks] == ["step one", "step two"]


def test_create_task_rejects_an_unreadable_due_date():
    setup_fake()
    result = run(S.create_task("x", board_id=BOARD, due_date="next tuesday"))
    assert result["ok"] is False and "ISO-8601" in result["error"]


def test_authoring_tools_refuse_someone_elses_task():
    fake = setup_fake()
    for call in (
        S.update_task_details("c2", description="mine now"),
        S.update_checklist("c2", add_items=["sneaky"]),
        S.attach_link("c2", "https://example.com", "link"),
    ):
        result = run(call)
        assert result["result"] == "not_yours", result
    assert not any(c.startswith("patch_card:c2") for c in fake.calls)
    assert fake.attachments == []


def test_update_task_details_edits_unclaimed_and_own_tasks():
    fake = setup_fake()
    result = run(S.update_task_details("c1", description="clearer", labels=["size: M"]))
    assert result["ok"] is True and result["changed"] == ["description"]
    assert fake.cards["c1"]["description"] == "clearer"
    assert result["labels_applied"] == ["size: M"]
    assert run(S.update_task_details("c1"))["result"] == "nothing_to_change"


def test_checklist_add_tick_and_reopen():
    fake = setup_fake()
    run(S.claim_task("c1"))
    run(S.update_checklist("c1", add_items=["write it", "test it"]))
    ticked = run(S.update_checklist("c1", complete_items=["Write It"]))  # case-insensitive
    assert ticked["toggled"] == ["Write It"]
    assert [t["isCompleted"] for t in fake.tasks] == [True, False]
    reopened = run(S.update_checklist("c1", reopen_items=["write it"], complete_items=["ghost"]))
    assert reopened["toggled"] == ["write it"]
    assert reopened["items_not_found"] == ["ghost"]
    assert fake.tasks[0]["isCompleted"] is False


def test_attach_link_requires_http_url():
    fake = setup_fake()
    bad = run(S.attach_link("c1", "file:///etc/passwd", "local"))
    assert bad["ok"] is False and "http" in bad["error"]
    assert fake.attachments == []
    good = run(S.attach_link("c1", "https://example.com/pr/42", "PR #42"))
    assert good["ok"] is True and fake.attachments[0]["url"] == "https://example.com/pr/42"


def test_the_only_delete_tool_is_the_guarded_one():
    names = {t.name for t in run(S.mcp.list_tools())}
    deleting = {n for n in names if "delete" in n or "remove" in n}
    # delete_list refuses non-empty lists; unassign only detaches people from a task
    assert deleting == {"delete_list"}
    assert "duplicate_task" not in names and "planka_request" not in names
    for expected in ("list_actionable_tasks", "claim_task", "assign_people",
                     "set_project_members", "manage_labels", "whoami"):
        assert expected in names


def test_api_keys_and_login_tokens_use_different_headers():
    from planka_mcp.client import PlankaClient
    from planka_mcp.config import Config

    keyed = PlankaClient(Config(base_url="https://x.test", api_key="secret-key"))
    assert keyed._auth_headers("secret-key") == {"X-Api-Key": "secret-key"}

    logged_in = PlankaClient(Config(base_url="https://x.test", email="a@b.c",
                                    password="pw"))
    assert logged_in._auth_headers("jwt-token") == {"Authorization": "Bearer jwt-token"}


# ---------------------------------------------------------------- find_tasks


def _titles(result):
    return {t["title"] for t in result["tasks"]}


def test_find_tasks_sees_other_peoples_work_unlike_the_queue():
    setup_fake()
    # the personal queue hides c2 because someone else holds it
    assert _titles(run(S.list_actionable_tasks())) == {"Ready task"}
    # search does not
    assert "Someone else's" in _titles(run(S.find_tasks()))


def test_find_tasks_filters_by_assignee_including_unassigned_and_me():
    fake = setup_fake()
    fake.instance_role = "admin"
    run(S.claim_task("c1"))

    mine = run(S.find_tasks(assignee="me"))
    assert _titles(mine) == {"Ready task"}

    theirs = run(S.find_tasks(assignee="Human"))
    assert _titles(theirs) == {"Someone else's"}

    nobody = run(S.find_tasks(assignee="unassigned"))
    assert "Ready task" not in _titles(nobody) and "Someone else's" not in _titles(nobody)


def test_find_tasks_refuses_an_ambiguous_person():
    fake = setup_fake()
    fake.instance_role = "admin"
    result = run(S.find_tasks(assignee="Hopper"))
    assert result["ok"] is False and result["result"] == "unresolved_assignee"
    assert len(result["candidates"]) == 2


def test_find_tasks_hides_done_work_unless_asked():
    setup_fake()
    assert "Shipped" not in _titles(run(S.find_tasks()))
    assert "Shipped" in _titles(run(S.find_tasks(include_done=True)))
    assert _titles(run(S.find_tasks(status="done"))) == {"Shipped"}


def test_find_tasks_matches_text_and_label_and_reports_filters():
    fake = setup_fake()
    fake.card_labels.append({"cardId": "c1", "labelId": "lb1"})

    assert _titles(run(S.find_tasks(text="do the thing"))) == {"Ready task"}  # description
    assert _titles(run(S.find_tasks(text="READY"))) == {"Ready task"}         # title, any case
    assert run(S.find_tasks(text="nothing here"))["total_matches"] == 0

    labelled = run(S.find_tasks(label="priority: high"))
    assert _titles(labelled) == {"Ready task"}
    assert labelled["filters"]["label"] == "priority: high"


def test_find_tasks_respects_the_board_allowlist():
    setup_fake(board_ids=["some-other-board"])
    scoped = run(S.find_tasks(project_id=BOARD))
    assert scoped["ok"] is False and "allowlist" in scoped["error"]
    assert run(S.find_tasks())["total_matches"] == 0  # nothing in scope to search


def test_find_tasks_caps_results_and_says_so():
    setup_fake()
    result = run(S.find_tasks(include_done=True, limit=1))
    assert result["count"] == 1 and result["total_matches"] > 1
    assert "narrow the filters" in result["note"]


def _involve(fake):
    """Give c3 a checklist item assigned to ADA and a comment mentioning GRACE."""
    fake.task_lists.append({"id": "tl-x", "cardId": "c3", "name": "Checklist"})
    fake.tasks.append({"id": "t-x", "taskListId": "tl-x", "name": "run the migration",
                       "assigneeUserId": ADA, "isCompleted": False})
    fake.cards["c3"]["commentsTotal"] = 1
    fake.comment_store["c3"] = [
        {"id": "cm1", "userId": OTHER,
         "text": f"@[Someone Renamed]({GRACE}) can you take a look?"}
    ]


def test_find_tasks_sees_checklist_assignments_not_just_card_membership():
    fake = setup_fake()
    fake.instance_role = "admin"
    _involve(fake)

    result = run(S.find_tasks(assignee="Ada", include_done=True))
    assert _titles(result) == {"Shipped"}          # c3, where ADA holds no card membership
    task = result["tasks"][0]
    assert task["matched_by"] == ["assigned a checklist item"]
    assert task["their_checklist_items"] == [{"name": "run the migration", "done": False}]
    assert result["how_they_are_involved"]["checklist items total"] == 1


def test_find_tasks_matches_mentions_by_user_id_so_renames_cannot_break_them():
    fake = setup_fake()
    fake.instance_role = "admin"
    _involve(fake)

    result = run(S.find_tasks(assignee="Grace", include_done=True))
    # the comment spells a different display name; the id is what matches
    assert _titles(result) == {"Shipped"}
    task = result["tasks"][0]
    assert task["matched_by"] == ["mentioned in a comment"]
    assert task["mentions"][0]["by"] == "Human"
    assert result["how_they_are_involved"]["mentioned in a comment"] == 1


def test_find_tasks_can_skip_the_comment_scan():
    fake = setup_fake()
    fake.instance_role = "admin"
    _involve(fake)
    quiet = run(S.find_tasks(assignee="Grace", include_done=True, include_mentions=False))
    assert quiet["total_matches"] == 0


def test_find_tasks_reports_every_way_a_person_is_involved():
    fake = setup_fake()
    fake.instance_role = "admin"
    # ADA holds the card, a checklist item on it, and is mentioned on it
    fake.memberships.append({"cardId": "c1", "userId": ADA, "createdAt": "2026-01-01"})
    fake.task_lists.append({"id": "tl-y", "cardId": "c1", "name": "Checklist"})
    fake.tasks.append({"id": "t-y", "taskListId": "tl-y", "name": "write it",
                       "assigneeUserId": ADA, "isCompleted": True})
    fake.cards["c1"]["commentsTotal"] = 1
    fake.comment_store["c1"] = [{"id": "cm2", "userId": OTHER, "text": f"ping @[A]({ADA})"}]

    task = run(S.find_tasks(assignee="ada@example.test"))["tasks"][0]
    assert task["matched_by"] == ["assigned to the task", "assigned a checklist item",
                                  "mentioned in a comment"]
    assert task["their_checklist_items"] == [{"name": "write it", "done": True}]


# ---------------------------------------------- full parameter coverage


def test_a_task_can_be_renamed_retyped_and_have_its_deadline_settled():
    fake = setup_fake()
    result = run(S.update_task_details("c1", title="Clearer title", task_type="story",
                                       due_date="2026-09-01T10:00:00Z", due_date_met=True))
    assert result["ok"] is True
    assert set(result["changed"]) == {"name", "type", "dueDate", "isDueCompleted"}
    assert fake.cards["c1"]["name"] == "Clearer title"
    assert fake.cards["c1"]["type"] == "story"
    assert fake.cards["c1"]["isDueCompleted"] is True

    bad = run(S.update_task_details("c1", task_type="epic"))
    assert bad["ok"] is False and "project, story or link" in bad["error"]


def test_create_task_chooses_its_kind_and_its_stage():
    fake = setup_fake()
    story = run(S.create_task("A story", board_id=BOARD, task_type="story"))
    assert story["type"] == "story" and fake.cards[story["task_id"]]["type"] == "story"

    parked = run(S.create_task("Parked", board_id=BOARD, into_list="Icebox"))
    assert parked["list"] == "Icebox" and parked["status"] == "not a work stage"

    nowhere = run(S.create_task("Nowhere", board_id=BOARD, into_list="Atlantis"))
    assert nowhere["ok"] is False and "No list called" in nowhere["error"]

    bad = run(S.create_task("Bad kind", board_id=BOARD, task_type="epic"))
    assert bad["ok"] is False


def test_checklist_items_can_be_given_an_owner():
    fake = setup_fake()
    fake.instance_role = "admin"
    run(S.claim_task("c1"))
    run(S.update_checklist("c1", add_items=["write it", "test it"]))

    result = run(S.update_checklist("c1", assign_items={"write it": "Ada"}))
    assert result["assigned"][0]["name"] == "Ada Hopper"
    assert next(t for t in fake.tasks if t["name"] == "write it")["assigneeUserId"] == ADA

    vague = run(S.update_checklist("c1", assign_items={"test it": "Hopper"}))
    assert vague["assigned"][0]["why"] == "no single match"
    assert len(vague["assigned"][0]["candidates"]) == 2

    ghost = run(S.update_checklist("c1", assign_items={"nothing": "Ada"}))
    assert ghost["items_not_found"] == ["nothing"]


def test_a_task_can_be_made_to_depend_on_another():
    fake = setup_fake()
    run(S.claim_task("c1"))
    result = run(S.update_checklist("c1", depends_on_tasks=["c2"]))   # c2 is unfinished
    assert result["depends_on"] == [{"task_id": "c2", "linked": True}]
    assert any(t.get("linkedCardId") == "c2" for t in fake.tasks)

    # the dependency shows up as something the task is waiting on
    detail = run(S.get_task("c1"))
    assert detail["task"]["waiting_on"][0]["card_id"] == "c2"

    # a link to already-finished work is not reported as blocking
    run(S.update_checklist("c1", depends_on_tasks=["c3"]))            # c3 is done
    assert [w["card_id"] for w in run(S.get_task("c1"))["task"]["waiting_on"]] == ["c2"]
    # and it is not counted as checklist work
    assert detail["task"]["subtasks"]["total"] == 0

    itself = run(S.update_checklist("c1", depends_on_tasks=["c1"]))
    assert itself["depends_on"][0]["why"] == "a task cannot depend on itself"


def test_a_checklist_item_can_be_added_and_assigned_in_one_call():
    fake = setup_fake()
    fake.instance_role = "admin"
    run(S.claim_task("c1"))
    result = run(S.update_checklist("c1", add_items=["fresh step"],
                                    assign_items={"fresh step": "Ada"}))
    assert result["added"] == ["fresh step"]
    assert result["assigned"][0]["name"] == "Ada Hopper"
    assert "items_not_found" not in result
    assert next(t for t in fake.tasks if t["name"] == "fresh step")["assigneeUserId"] == ADA
