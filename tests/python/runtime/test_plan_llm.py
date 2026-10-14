"""The task board written and revised by the model, not split from wording."""
import json
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _convo, _limits, _plan_llm, _tasks


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_LLM_PLAN", "1")


def _model(monkeypatch, reply, seen=None):
    from aiforge_core.llm import client

    def fake(role, messages, **kw):
        if seen is not None:
            seen.append((role, messages, kw))
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(client, "complete", fake)


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


MSG = ("add a /health endpoint to repo-a and repo-b, and in between update the "
       "shared client in libs/common so both use it")


def test_the_plan_is_the_models_refined_list_with_earlier_context(monkeypatch):
    seen = []
    tasks = ["Add /health to repo-a/api.py — check: curl :8080/health is 200",
             "Update libs/common client to call /health — check: its tests pass",
             "Add /health to repo-b/api.py — check: curl :8081/health is 200"]
    _model(monkeypatch, "thinking… " + json.dumps({"tasks": tasks}), seen)
    messages = [{"role": "user", "content": "look at repo-a and repo-b"},
                {"role": "assistant", "content": "Both are FastAPI services."},
                {"role": "user", "content": MSG}]
    got = _convo._task_list("chat", MSG, messages, "/w", 7, act=True)
    assert got == tasks
    asked = seen[0][1][1]["content"]
    assert "Both are FastAPI services." in asked and MSG in asked
    assert "/w" in asked and seen[0][0] == "chat"


def test_a_question_gets_no_board_and_the_splitter_is_not_used(monkeypatch):
    _model(monkeypatch, '{"tasks": []}')
    msg = "why does the build fail? also what does make do here?"
    assert _convo._task_list("chat", msg, [], None, 7, act=True) == []


def test_a_one_item_plan_is_no_board(monkeypatch):
    _model(monkeypatch, '{"tasks": ["rename foo to bar — check: tests pass"]}')
    assert _convo._task_list("chat", "please rename foo to bar in utils.py", [],
                             None, 7, act=True) == []


@pytest.mark.parametrize("reply", [RuntimeError("down"), "no json here", '{"steps": []}'])
def test_a_failed_call_falls_back_to_the_splitter(monkeypatch, reply):
    _model(monkeypatch, reply)
    msg = "fix the login bug. also add a test for the logout path please"
    assert _convo._task_list("chat", msg, [], None, 7, act=True) == \
        _convo._split_asks(msg)


def test_not_asked_off_act_or_on_a_harness_turn(monkeypatch):
    seen = []
    _model(monkeypatch, '{"tasks": ["a — check: x", "b — check: y"]}', seen)
    msg = "fix the login bug. also add a test for the logout path please"
    _convo._task_list("chat", msg, [], None, 7, act=False)
    _convo._task_list("chat", msg, [], None, None, act=True)
    _convo._task_list("chat", "⟳ A background command finished — continuing. " + msg,
                      [], None, 7, act=True)
    monkeypatch.setenv("AIFORGE_CHAT_LLM_PLAN", "0")
    _convo._task_list("chat", msg, [], None, 7, act=True)
    assert seen == []
    assert _convo._task_list("chat", "continue", [], None, 7, act=True) == []


def _board():
    return _tasks.seed_board(["fix repo-a login — check: tests pass",
                              "fix repo-b login — check: tests pass"])


def test_revise_inserts_in_between_changes_and_drops(monkeypatch):
    board = _board()
    board["part-1"]["status"] = "done"
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": board["part-1"]["title"], "status": "done"},
        {"id": None, "title": "update libs/common — check: its tests pass",
         "status": "pending"},
        {"id": "part-2", "title": "fix repo-b login and logout — check: tests pass",
         "status": "pending"},
        {"id": "part-9", "title": "write CHANGELOG entry — check: file has it",
         "status": "done"},
    ]}))
    new, changes = _plan_llm.revise("chat", board, ["in between update libs/common"],
                                    "fix login in both repos", 7)
    assert list(new) == ["part-1", "msg-3", "part-2", "msg-4"]
    assert new["msg-3"]["status"] == "pending" and new["msg-4"]["status"] == "pending"
    assert new["part-1"]["status"] == "done"
    assert "logout" in new["part-2"]["title"]
    assert any(c.startswith("added msg-3") for c in changes)
    assert any(c.startswith("changed part-2") for c in changes)


def test_revise_keeps_left_out_items_and_never_marks_done(monkeypatch):
    board = _board()
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-2", "title": board["part-2"]["title"], "status": "done"}]}))
    new, changes = _plan_llm.revise("chat", board, ["skip nothing"], "goal", 7)
    assert changes == [] and new is board
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": board["part-1"]["title"], "status": "skipped"}]}))
    new, changes = _plan_llm.revise("chat", board, ["forget repo-a"], "goal", 7)
    # Dropped by the user: off the board (10-15: not kept as a struck line).
    assert list(new) == ["part-2"] and new["part-2"]["status"] == "pending"
    assert any(c.startswith("removed part-1") for c in changes)


def test_revise_keeps_a_reorder_and_ignores_an_added_check(monkeypatch):
    board = _tasks.seed_board(["fix repo-a", "fix repo-b", "run tests"])
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": "fix repo-a — check: tests pass", "status": "pending"},
        {"id": "part-3", "title": "run tests", "status": "pending"},
        {"id": "part-2", "title": "fix repo-b", "status": "pending"}]}))
    new, changes = _plan_llm.revise("chat", board, ["run tests before repo-b"], "g", 7)
    assert list(new) == ["part-1", "part-3", "part-2"]
    assert new["part-1"]["title"] == "fix repo-a"
    assert changes == ["new order: part-1, part-3, part-2"]
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": s, "title": it["title"] + " — check: ok", "status": it["status"]}
        for s, it in board.items()]}))
    assert _plan_llm.revise("chat", board, ["looks good"], "g", 7) == (board, [])
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": "fix repo-a and repo-c", "status": "pending"}]}))
    new, changes = _plan_llm.revise("chat", board, ["repo-c too"], "g", 7)
    assert new["part-1"]["title"] == "fix repo-a and repo-c" and changes


def test_plan_reads_object_items_and_an_unreadable_shape_falls_back(monkeypatch):
    _model(monkeypatch, json.dumps({"tasks": [{"title": "a — check: x"},
                                              {"title": "b — check: y"}]}))
    assert _plan_llm.plan_asks("chat", MSG, []) == ["a — check: x", "b — check: y"]
    _model(monkeypatch, json.dumps({"tasks": [{"name": "a"}, {"name": "b"}]}))
    assert _plan_llm.plan_asks("chat", MSG, []) is None


def test_a_resume_brief_under_the_request_is_not_planned(monkeypatch):
    seen = []
    _model(monkeypatch, '{"tasks": []}', seen)
    _convo._task_list("chat", MSG + "\n\n---\n[RESUME] Subtasks already completed: x",
                      [], None, 7, act=True)
    assert "RESUME" not in seen[0][1][1]["content"]


def test_revise_failure_keeps_the_board(monkeypatch):
    _model(monkeypatch, RuntimeError("down"))
    assert _plan_llm.revise("chat", _board(), ["also do x"], "goal", 7) == (None, [])


def _st(**kw):
    base = dict(board=_board(), session_id=7, role="chat", goal="fix login",
                builder=None, strict_finish=False, plan_mode=False,
                readonly_mode=False)
    return SimpleNamespace(**{**base, **kw})


def test_a_steer_updates_the_board_and_the_dock(monkeypatch):
    st = _st()
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": st.board["part-1"]["title"], "status": "pending"},
        {"id": None, "title": "add rate limit to repo-a — check: test passes",
         "status": "pending"},
        {"id": "part-2", "title": st.board["part-2"]["title"], "status": "pending"}]}))
    events = []
    thoughts, note = _drain(_limits._revise_board(st, ["also add a rate limit"], events))
    assert list(st.board) == ["part-1", "msg-3", "part-2"] and st.board_used
    assert events and events[0]["type"] == "subtasks" and len(events[0]["items"]) == 3
    assert "added msg-3" in note and "rate limit" in note
    assert thoughts and "task list" in thoughts[0]["text"]


def test_a_steer_that_changes_nothing_or_fails_tells_the_model(monkeypatch):
    st = _st()
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": s, "title": it["title"], "status": it["status"]} for s, it in st.board.items()]}))
    events = []
    _, note = _drain(_limits._revise_board(st, ["how is it going?"], events))
    assert events == [] and "changes nothing" in note
    _model(monkeypatch, RuntimeError("down"))
    _, note = _drain(_limits._revise_board(st, ["also x"], events))
    assert events == [] and note == _tasks.steer_note(st.board)


def test_a_steer_on_a_turn_without_a_board_can_start_one(monkeypatch):
    st = _st(board={})
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": None, "title": "fix login — check: tests pass", "status": "pending"},
        {"id": None, "title": "then deploy to staging — check: /health 200",
         "status": "pending"}]}))
    events = []
    _, note = _drain(_limits._revise_board(st, ["after that deploy to staging"], events))
    assert list(st.board) == ["msg-1", "msg-2"] and events
    assert _tasks.open_planned(st.board) == ["msg-1", "msg-2"]


def test_plan_mode_steer_is_not_revised(monkeypatch):
    seen = []
    _model(monkeypatch, '{"tasks": []}', seen)
    st = _st(plan_mode=True)
    _, note = _drain(_limits._revise_board(st, ["also x"], []))
    assert seen == [] and note == _tasks.steer_note(st.board)


def test_continue_plans_the_quoted_request_and_a_pasted_rule_is_kept(monkeypatch):
    from aiforge_core.runtime.chat_resume import REQUEST_CLOSE, REQUEST_OPEN
    seen = []
    _model(monkeypatch, '{"tasks": []}', seen)
    brief = (f"continue\n\n---\n[RESUME] stopped.\n{REQUEST_OPEN}\n{MSG}\n{REQUEST_CLOSE}\n"
             "Subtasks already completed: x")
    _convo._task_list("chat", brief, [], None, 7, act=True)
    asked = seen[-1][1][1]["content"]
    assert MSG in asked and "Subtasks already completed" not in asked
    toml = "fix the ruff config below so lint passes\n---\n[tool.ruff]\nline-length = 1"
    _convo._task_list("chat", toml, [], None, 7, act=True)
    assert "[tool.ruff]" in seen[-1][1][1]["content"]


def test_an_unchanged_title_with_other_spacing_is_no_change(monkeypatch):
    board = {"part-1": {"title": "fix  repo-a", "status": "pending"},
             "part-2": {"title": "fix repo-b", "status": "pending"}}
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": "fix repo-a", "status": "pending"},
        {"id": "part-2", "title": "fix repo-b", "status": "pending"}]}))
    assert _plan_llm.revise("chat", board, ["ok"], "g", 7) == (board, [])


LIVE_PLAN = [
    "Add a farewell(name) function in svc-a that returns 'bye ' + name — check: the function exists in svc-a",
    "Add a pytest test in svc-a that asserts farewell(name) returns 'bye ' + name — check: the test passes",
    "Write a NOTES.md in /home/ai/work/repos describing what was changed in svc-a — check: NOTES.md exists",
    "Add a farewell(name) function in svc-b that returns 'bye ' + name — check: the function exists in svc-b",
    "Add a pytest test in svc-b that asserts farewell(name) returns 'bye ' + name — check: the test passes",
    "Run the pytest tests in both svc-a and svc-b — check: all tests pass in both repositories",
]


def test_the_models_own_replan_maps_onto_the_planned_items():
    """Seen live: the working model wrote the same plan again in its own words
    and the board doubled, the first copy left open."""
    board = _tasks.seed_board(LIVE_PLAN)
    own = {"svc-a-function": ("svc-a: add farewell(name) returning 'bye ' + name "
                              "(done when function in app.py)", "part-1"),
           "svc-a-test": ("svc-a: add pytest test for farewell (done when test_app.py "
                          "test passes)", "part-2"),
           "svc-b-function": ("svc-b: add farewell(name) returning 'bye ' + name "
                              "(done when function in app.py)", "part-4"),
           "svc-b-test": ("svc-b: add pytest test for farewell (done when test_app.py "
                          "test passes)", "part-5"),
           "notes-md": ("Write NOTES.md in root describing svc-a changes "
                        "(done when file exists)", "part-3")}
    for slug, (title, want) in own.items():
        result, events = _tasks.apply_progress(board, {"slug": slug, "title": title,
                                                       "status": "pending"})
        assert result["slug"] == want and want in result["note"], slug
    assert len(board) == 6
    result, events = _tasks.apply_progress(board, {"slug": "svc-b-test", "status": "done"})
    assert result["slug"] == "part-5" and board["part-5"]["status"] == "done"
    assert events[0]["slug"] == "part-5"
    # New work in its own words still goes on the board.
    _tasks.apply_progress(board, {"slug": "lint", "title": "run ruff on svc-a and svc-b"})
    _tasks.apply_progress(board, {"slug": "c-fn", "title": "add farewell(name) to svc-c"})
    assert "lint" in board and "c-fn" in board


def test_a_redo_is_held_at_final_and_not_closed_for_the_agent(monkeypatch):
    board = _board()
    board["part-1"]["status"] = "done"
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "part-1", "title": board["part-1"]["title"], "status": "pending"},
        {"id": "part-2", "title": board["part-2"]["title"], "status": "pending"}]}))
    new, changes = _plan_llm.revise("chat", board, ["redo repo-a, it broke"], "g", 7)
    assert new["part-1"]["status"] == "pending" and new["part-1"]["redo"]
    assert _tasks.open_planned(new) == ["part-1"]
    assert any(c.startswith("to do again part-1") for c in changes)


def test_the_plan_call_says_so_and_is_bounded_by_its_timeout(monkeypatch):
    from aiforge_core.runtime import chat_runs
    said, phases, seen = [], [], []
    monkeypatch.setattr(chat_runs, "publish", lambda sid, ev: said.append(ev))
    monkeypatch.setattr(chat_runs, "set_phase", lambda sid, t: phases.append(t))
    monkeypatch.setenv("AIFORGE_CHAT_LLM_PLAN_TIMEOUT_S", "12")
    _model(monkeypatch, '{"tasks": []}', seen)
    _convo._task_list("chat", MSG, [], None, 7, act=True)
    assert said and "task list" in said[0]["text"] and phases == ["writing the task list"]
    # The request's own deadline is the wait: nothing outlives it.
    assert seen[0][2]["timeout_s"] == 12


def test_a_message_mid_run_changes_the_approved_plans_steps(monkeypatch):
    """Wanted (user 10-11): during an approved plan, what the user types
    changes the plan's steps, not only a side note."""
    goal = ("Carry out the approved plan.\n\n1. add farewell to svc-a\n"
            "2. add farewell to svc-b\n3. run the tests in both")
    st = _st(board=_tasks.seed_board([], goal), goal=goal)
    assert list(st.board) == ["plan-1", "plan-2", "plan-3"]
    _model(monkeypatch, json.dumps({"tasks": [
        {"id": "plan-1", "title": "add farewell to svc-a", "status": "pending"},
        {"id": "plan-2", "title": "add farewell to svc-b", "status": "skipped"},
        {"id": None, "title": "add farewell to svc-c — check: its test passes",
         "status": "pending"},
        {"id": "plan-3", "title": "run the tests in svc-a and svc-c", "status": "pending"}]}))
    events = []
    _, note = _drain(_limits._revise_board(st, ["not svc-b, do svc-c instead"], events))
    assert list(st.board) == ["plan-1", "msg-3", "plan-3"]
    assert "plan-2" not in st.board
    assert "svc-c" in st.board["plan-3"]["title"] and st.board_used and events
    assert "replaces any checklist" in note
