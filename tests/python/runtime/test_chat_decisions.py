# tests/python/runtime/test_chat_decisions.py
"""What changed the course of a chat's work is kept, one line each, for the
Run overview: a message that changed the goals or the task list, a task the
agent added, skipped or took off, a choice it reported, tasks a turn left."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.runtime import chat_decisions as D
from aiforge_core.runtime.chat_agent._turn import _action, _limits, _tasks


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_DECISIONS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(D, "_temporary", lambda sid: False)
    monkeypatch.setattr(D, "_user_turns", lambda sid: 2)
    D._MEM.clear()


def test_rows_are_kept_in_order_capped_and_forgotten():
    D.record(1, ["first", "", "second"], by="you", kind="plan", why="  do  x ")
    D.record(1, "third", by="agent", kind="choice")
    rows = D.rows(1)
    assert [r["text"] for r in rows] == ["first", "second", "third"]
    assert rows[0]["why"] == "do x" and rows[0]["turn"] == 2 and "why" not in rows[2]
    for i in range(D.MAX_ROWS + 5):
        D.record(1, f"r{i}", by="harness", kind="plan")
    assert len(D.rows(1)) == D.MAX_ROWS and D.rows(1)[-1]["text"] == f"r{D.MAX_ROWS + 4}"
    D.forget(1)
    assert D.rows(1) == []


def test_off_switch_and_no_session_record_nothing(monkeypatch):
    D.record(None, "x", by="you", kind="plan")
    monkeypatch.setenv("AIFORGE_CHAT_DECISIONS", "0")
    D.record(1, "x", by="you", kind="plan")
    monkeypatch.setenv("AIFORGE_CHAT_DECISIONS", "1")
    assert D.rows(1) == []


def test_a_temporary_chat_keeps_rows_in_memory_only(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_temporary", lambda sid: True)
    D.record(5, "x", by="you", kind="plan")
    assert D.rows(5)[0]["text"] == "x"
    assert not (tmp_path / "ws").exists()


def test_edit_and_resend_takes_back_the_later_turns_rows(monkeypatch):
    monkeypatch.setattr(D, "_user_turns", lambda sid: 1)
    D.record(1, "kept", by="you", kind="plan")
    monkeypatch.setattr(D, "_user_turns", lambda sid: 3)
    D.record(1, "gone", by="you", kind="plan")
    D.drop_after_turn(1, 2)
    assert [r["text"] for r in D.rows(1)] == ["kept"]


def test_lines_read_in_the_users_terms():
    assert D.board_lines(["added msg-3: add x", "removed s2: y", "new order: a, b"]) == [
        "Added task msg-3: add x", "Removed task s2: y", "New task order: a, b"]
    assert D.goal_lines(["dropped g1: z", "reopened g2"]) == [
        "Goal dropped g1: z", "Goal reopened g2"]


def _st(board):
    return SimpleNamespace(board=board, session_id=1)


def test_agent_board_changes_are_recorded():
    board = _tasks.seed_board(["fix login — check: tests pass"])
    st = _st(board)
    before = {s: it.get("status") for s, it in board.items()}
    res, _ = _tasks.apply_progress(board, {"slug": "lint", "title": "tidy imports"},
                                   context_words={"login"})
    _action._note_decisions(st, {"slug": "lint", "title": "tidy imports"}, res, before)
    before = {s: it.get("status") for s, it in board.items()}
    args = {"slug": "part-1", "status": "skipped",
            "decision": "login already fixed upstream"}
    res, _ = _tasks.apply_progress(board, args)
    _action._note_decisions(st, args, res, before)
    before = {s: it.get("status") for s, it in board.items()}
    res, _ = _tasks.apply_progress(board, {"slug": "lint", "remove": True})
    _action._note_decisions(st, {"slug": "lint", "remove": True}, res, before)
    rows = D.rows(1)
    texts = [r["text"] for r in rows]
    assert texts[0].startswith("Agent added a task of its own — lint")
    assert texts[1] == "Agent marked part-1 skipped: fix login — check: tests pass"
    assert texts[2] == "login already fixed upstream (part-1)" and rows[2]["kind"] == "choice"
    assert texts[3] == "Agent took lint off the task list"
    assert {r["by"] for r in rows} == {"agent"}


def test_a_refused_or_plain_progress_call_records_nothing():
    board = _tasks.seed_board(["fix login — check: tests pass"])
    st = _st(board)
    before = {s: it.get("status") for s, it in board.items()}
    _action._note_decisions(st, {"slug": "x"}, {"ok": False}, before)
    res, _ = _tasks.apply_progress(board, {"slug": "part-1", "status": "running"})
    _action._note_decisions(st, {"slug": "part-1", "status": "running"}, res, before)
    assert D.rows(1) == []


def test_a_steer_that_revises_the_board_is_recorded_with_the_message(monkeypatch):
    from aiforge_core.llm import client
    monkeypatch.setenv("AIFORGE_CHAT_LLM_PLAN", "1")
    board = _tasks.seed_board(["fix repo-a login — check: tests pass",
                               "fix repo-b login — check: tests pass"])
    st = SimpleNamespace(board=board, session_id=1, role="chat", goal="fix login",
                         builder=None, strict_finish=False, plan_mode=False,
                         readonly_mode=False)
    reply = json.dumps({"tasks": [
        {"id": "part-1", "title": board["part-1"]["title"], "status": "pending"},
        {"id": None, "title": "add rate limit — check: test passes", "status": "pending"},
        {"id": "part-2", "title": board["part-2"]["title"], "status": "pending"}]})
    monkeypatch.setattr(client, "complete", lambda role, messages, **kw: reply)
    gen = _limits._revise_board(st, ["also add a rate limit"], [])
    for _ in gen:
        pass
    rows = D.rows(1)
    assert rows and rows[0]["text"].startswith("Added task msg-3: add rate limit")
    assert rows[0]["by"] == "you" and rows[0]["why"] == "also add a rate limit"


def test_tasks_left_open_at_turn_end_are_recorded():
    from aiforge_core.api.routes._chat import _turn_events
    run = SimpleNamespace(publish=lambda ev: None)
    st = {"subtasks": [{"slug": "a", "status": "done", "title": "A"},
                       {"slug": "b", "status": "running", "title": "B"}]}
    _turn_events._settle_task_list(st, run, 1)
    rows = D.rows(1)
    assert len(rows) == 1 and rows[0]["kind"] == "left" and "b: B" in rows[0]["text"]
    _turn_events._settle_task_list(st, run, 1)          # nothing open now
    assert len(D.rows(1)) == 1


def test_overview_route_has_goals_and_decisions(monkeypatch):
    from aiforge_core.api.routes import chat as ch
    from aiforge_core.runtime import chat_goals, chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"id": sid} if sid == 1 else None)
    monkeypatch.setattr(chat_goals, "goals", lambda sid: {
        "goals": [{"id": "g1", "text": "build login", "status": "open", "updates": [1]}],
        "now": ["g1"], "current": "build login", "asked": ""})
    D.record(1, "Added task msg-1: x", by="you", kind="plan")
    app = FastAPI()
    app.include_router(ch.router)
    c = TestClient(app)
    body = c.get("/api/chat/sessions/1/overview").json()
    assert body["goals"] == [{"id": "g1", "text": "build login", "status": "open"}]
    assert body["now"] == ["g1"] and body["current"] == "build login"
    assert body["decisions"][0]["text"] == "Added task msg-1: x"
    assert c.get("/api/chat/sessions/2/overview").status_code == 404


def test_a_refused_call_still_keeps_the_choice_it_reports():
    st = _st({})
    _action._note_decisions(st, {"decision": "kept sqlite"}, {"ok": False}, {})
    assert [r["text"] for r in D.rows(1)] == ["kept sqlite"]


def test_a_steer_that_changes_the_goals_or_asks_is_recorded(monkeypatch):
    from aiforge_core.runtime import chat_goals
    monkeypatch.setattr(chat_goals, "enabled", lambda: True)
    st = SimpleNamespace(board={}, role="chat", builder=None, strict_finish=False)
    monkeypatch.setattr(chat_goals, "on_message", lambda *a, **k: {
        "changes": ["changed g1: build login with dark mode"], "current": "x"})
    _limits._steer_goals(st, 1, ["also dark mode"], [])
    monkeypatch.setattr(chat_goals, "on_message", lambda *a, **k: {"question": "which one?"})
    _limits._steer_goals(st, 1, ["drop it"], [])
    rows = D.rows(1)
    assert rows[0]["text"] == "Goal changed g1: build login with dark mode"
    assert rows[0]["why"] == "also dark mode" and rows[0]["by"] == "you"
    assert rows[1]["text"] == "Asked you: which one?" and rows[1]["kind"] == "question"


def test_the_first_goal_row_does_not_repeat_the_message():
    D.record(1, "Goal added g1: fix login", by="you", kind="goal", why="fix login")
    assert "why" not in D.rows(1)[0]


def test_the_same_line_twice_in_a_row_is_kept_once():
    D.record(1, "kept sqlite", by="agent", kind="choice")
    D.record(1, "kept sqlite", by="agent", kind="choice")
    D.record(1, "other", by="agent", kind="choice")
    D.record(1, "kept sqlite", by="agent", kind="choice")
    assert [r["text"] for r in D.rows(1)] == ["kept sqlite", "other", "kept sqlite"]
