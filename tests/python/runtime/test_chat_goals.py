# tests/python/runtime/test_chat_goals.py
"""A chat keeps every goal the user set: a steer or a new message changes the
goals, it does not replace them; an unplaceable message is asked about."""
import json
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import chat_goals as G


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_GOALS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(G, "_temporary", lambda sid: False)
    G._MEM.clear()


def _model(monkeypatch, *replies):
    """The goal model answers with ``replies`` in turn; the prompts it got
    are returned."""
    seen: list = []
    queue = list(replies)

    def ask(role, user, sid):
        seen.append(user)
        r = queue.pop(0) if queue else None
        return r if (r is None or isinstance(r, str)) else json.dumps(r)
    monkeypatch.setattr(G, "_ask", ask)
    return seen


def test_first_message_is_the_goal_without_a_model_call(monkeypatch):
    seen = _model(monkeypatch)
    out = G.on_message(1, "build a login page in web/", turn=1)
    assert out["current"] == "build a login page in web/" and not out["called"]
    assert not seen
    assert "GOAL NOW: build a login page in web/" in G.block(1)


def test_a_steer_changes_the_goal_and_keeps_it(monkeypatch):
    G.on_message(1, "build a login page in web/", turn=1)
    seen = _model(monkeypatch, {
        "goals": [{"id": "g1", "status": "open",
                   "text": "build a login page in web/ with a dark mode"}],
        "now": ["g1"], "current": "build a login page in web/ with a dark mode",
        "question": ""})
    out = G.on_message(1, "also add a dark mode", mid_run=True, turn=1)
    assert "login page" in out["current"] and "dark mode" in out["current"]
    assert "build a login page in web/" in seen[0]          # the model saw the goal
    blk = G.block(1)
    assert "dark mode" in blk and "later the user said" in blk


def test_a_goal_the_reply_leaves_out_is_kept(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": None, "status": "open",
                                    "text": "write the README"}],
                         "now": ["new"], "current": "write the README",
                         "question": ""})
    G.finish_turn(1, completed=False)
    G.on_message(1, "and write the README", turn=2)
    data = G.goals(1)
    assert [g["status"] for g in data["goals"]] == ["open", "open"]
    assert data["now"] == ["g2"]
    blk = G.block(1)
    assert "g1 (open, not this turn): build a login page" in blk
    assert "still to do" in blk


def test_only_the_users_words_drop_a_goal(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "dropped", "text": "x"},
                                   {"id": None, "status": "open",
                                    "text": "build a signup page"}],
                         "now": ["new"], "current": "build a signup page",
                         "question": ""})
    out = G.on_message(1, "forget the login page, do a signup page instead",
                       mid_run=True, turn=1)
    assert any(c.startswith("dropped g1") for c in out["changes"])
    assert "login" not in G.block(1)


def test_a_failed_model_call_drops_nothing(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    _model(monkeypatch, None)
    out = G.on_message(1, "use postgres for sessions", mid_run=True, turn=1)
    assert "build a login page" in out["current"] and "postgres" in out["current"]
    assert G.goals(1)["goals"][0]["status"] == "open"


def test_unclear_message_gives_one_question_then_the_answer_settles_it(monkeypatch):
    G.on_message(1, "migrate service A to java 21", turn=1)
    _model(monkeypatch,
           {"goals": [{"id": "g1", "status": "open", "text": "migrate service A to java 21"}],
            "now": ["g1"], "current": "migrate service A to java 21",
            "question": "Should B replace A, or be migrated as well?"},
           {"goals": [{"id": "g1", "status": "open", "text": "migrate A and B to java 21"}],
            "now": ["g1"], "current": "migrate A and B to java 21",
            "question": "Still unsure?"})
    out = G.on_message(1, "do B", turn=2)
    assert out["question"].startswith("Should B replace A")
    assert "Waiting on the user's answer" in G.block(1)
    # A turn waiting on the answer closes nothing.
    G.finish_turn(1, completed=True)
    assert G.goals(1)["goals"][0]["status"] == "open"
    out = G.on_message(1, "as well", turn=3)
    assert out["question"] == ""                  # never asked twice in a row
    assert out["current"] == "migrate A and B to java 21"


def test_a_finished_turn_closes_its_goals_and_a_follow_up_reopens(monkeypatch):
    G.on_message(1, "get the backtest to 100% CAGR", turn=1)
    G.finish_turn(1, completed=True)
    assert G.goals(1)["goals"][0]["status"] == "done"
    assert "(done earlier)" in G.block(1)
    seen = _model(monkeypatch, {
        "goals": [{"id": "g1", "status": "open",
                   "text": "get the backtest to 100% CAGR using leverage"}],
        "now": ["g1"], "current": "get the backtest to 100% CAGR using leverage",
        "question": ""})
    out = G.on_message(1, "try leverage", said="CAGR is 60%", turn=2)
    assert out["called"] and "100% CAGR" in out["current"]
    assert "CAGR is 60%" in seen[0]
    assert G.goals(1)["goals"][0]["status"] == "open"


def test_a_done_goal_copied_as_open_is_not_reopened(monkeypatch):
    G.on_message(1, "write the report", turn=1)
    G.finish_turn(1, completed=True)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "write the report"},
                                   {"id": None, "status": "open", "text": "fix the login bug"}],
                         "now": ["new"], "current": "fix the login bug", "question": ""})
    G.on_message(1, "now fix the login bug", turn=2)
    assert [g["status"] for g in G.goals(1)["goals"]] == ["done", "open"]


def test_stopped_turn_keeps_goals_open_and_continue_costs_no_call(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    G.finish_turn(1, completed=False)
    seen = _model(monkeypatch)
    out = G.on_message(1, "continue", turn=2)
    assert not seen and out["current"] == "build a login page"


def test_edit_resend_takes_back_later_goals_and_changes(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open",
                                    "text": "build a login page with SSO"},
                                   {"id": None, "status": "open", "text": "write docs"}],
                         "now": ["g1", "new"], "current": "login with SSO + docs",
                         "question": ""})
    G.on_message(1, "add SSO and write docs", turn=2)
    G.drop_after_turn(1, 1)
    data = G.goals(1)
    assert [g["text"] for g in data["goals"]] == ["build a login page"]


def test_forget_and_off_switch(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    G.forget(1)
    assert G.block(1) == ""
    monkeypatch.setenv("AIFORGE_CHAT_GOALS", "0")
    assert G.on_message(1, "anything", turn=1)["current"] == ""
    assert G.block(1) == ""


def test_turn_pin_carries_the_goals(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn._tasks import turn_pin
    G.on_message(7, "build a login page", turn=1)
    st = SimpleNamespace(goal="also add dark mode", steers=[], file_hashes={},
                         cwd="/tmp", session_id=7, unlimited=False,
                         readonly_mode=False, builder=None, item_resets=0,
                         failed_approaches=[], strict_finish=False)
    pin = turn_pin(st)
    assert "GOALS IN THIS CHAT" in pin and "build a login page" in pin


def test_steer_drain_tells_the_agent_the_whole_goal(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _limits
    G.on_message(3, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open",
                                    "text": "build a login page with dark mode"}],
                         "now": ["g1"], "current": "build a login page with dark mode",
                         "question": ""})
    st = SimpleNamespace(role="chat", builder=None, strict_finish=False, turn_no=1)
    events: list = []
    note, asking = _limits._steer_goals(st, 3, ["also dark mode"], events)
    assert not asking and "GOAL NOW" in note and "login page with dark mode" in note
    assert events and events[0]["role"] == "goals"


def test_steer_drain_asks_when_unclear(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _limits
    G.on_message(3, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "build a login page"}],
                         "now": ["g1"], "current": "build a login page",
                         "question": "Replace the login page with it, or add it?"})
    st = SimpleNamespace(role="chat", builder=None, strict_finish=False, turn_no=1)
    note, asking = _limits._steer_goals(st, 3, ["do the other page"], [])
    assert asking and "ASK: Replace the login page" in note


def test_a_vague_message_that_would_drop_every_goal_is_confirmed(monkeypatch):
    G.on_message(1, "add power() with a test, and csv() with a test", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "dropped", "text": "x"}],
                         "now": [], "current": "", "question": ""})
    out = G.on_message(1, "skip that one, not needed anymore", mid_run=True, turn=1)
    assert out["question"].startswith("To be sure: should I stop all of this")
    assert G.goals(1)["goals"][0]["status"] == "open"          # nothing dropped yet


def test_an_explicit_cancel_drops_without_asking(monkeypatch):
    G.on_message(1, "add power() with a test", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "dropped", "text": "x"}],
                         "now": [], "current": "", "question": ""})
    out = G.on_message(1, "cancel all of it", mid_run=True, turn=1)
    assert out["question"] == "" and G.goals(1)["goals"][0]["status"] == "dropped"


def test_the_step_in_progress_reaches_the_goal_model(monkeypatch):
    from aiforge_core.runtime.chat_agent._turn import _limits
    G.on_message(3, "add power() and csv()", turn=1)
    seen = _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "add power()"}],
                                "now": ["g1"], "current": "add power()", "question": ""})
    st = SimpleNamespace(role="chat", builder=None, strict_finish=False,
                         board={"part-1": {"title": "add power()", "status": "done"},
                                "part-2": {"title": "add csv()", "status": "running"}})
    _limits._steer_goals(st, 3, ["skip that one"], [])
    assert "RIGHT NOW: add csv()" in seen[0]


def test_a_done_goal_is_never_dropped(monkeypatch):
    G.on_message(1, "write the report", turn=1)
    G.finish_turn(1, completed=True)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "dropped", "text": "write the report"}],
                         "now": [], "current": "", "question": ""})
    G.on_message(1, "the report is fine, thanks", turn=2)
    assert G.goals(1)["goals"][0]["status"] == "done"


def test_a_vague_drop_with_two_goals_asks_which(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch,
           {"goals": [{"id": None, "status": "open", "text": "add csv()"}],
            "now": ["new"], "current": "add csv()", "question": ""},
           {"goals": [{"id": "g2", "status": "dropped", "text": "add csv()"}],
            "now": [], "current": "", "question": ""})
    G.on_message(1, "separately, add csv()", turn=2)
    out = G.on_message(1, "drop it", turn=3)
    assert out["question"].startswith("Which one do you mean: (1) add power()")
    assert [g["status"] for g in G.goals(1)["goals"]] == ["open", "open"]


def test_a_new_turn_after_a_stop_is_told_the_old_goal_is_unfinished(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    seen = _model(monkeypatch, None)
    G.on_message(1, "separately, add csv()", turn=2)
    assert "THE LAST TURN STOPPED BEFORE FINISHING: g1" in seen[0]


def test_a_message_about_no_goal_closes_nothing(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "add power()"}],
                         "now": [], "current": "", "question": ""})
    G.on_message(1, "how long will this take?", turn=2)
    G.finish_turn(1, completed=True)
    assert G.goals(1)["goals"][0]["status"] == "open"


def test_a_vague_order_with_one_goal_does_not_ask(monkeypatch):
    G.on_message(1, "add power() and csv()", turn=1)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "add power()"}],
                         "now": ["g1"], "current": "add power()", "question": ""})
    out = G.on_message(1, "skip that one", mid_run=True, turn=1)
    assert out["question"] == "" and out["current"] == "add power()"


def test_a_mid_run_addition_keeps_the_goal_in_hand(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    _model(monkeypatch, {"goals": [{"id": None, "status": "open", "text": "add a dark mode"}],
                         "now": ["new"], "current": "add a dark mode", "question": ""})
    out = G.on_message(1, "also add a dark mode", mid_run=True, turn=1)
    assert G.goals(1)["now"] == ["g1", "g2"]
    assert "build a login page" in out["current"] and "dark mode" in out["current"]


def test_a_failed_call_after_a_stop_keeps_the_stopped_goal(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch, None)
    out = G.on_message(1, "use postgres instead", turn=2)
    assert "build a login page" in out["current"] and "postgres" in out["current"]
    assert [g["id"] for g in G.goals(1)["goals"]] == ["g1"]


def test_stop_during_the_goal_call_keeps_nothing(monkeypatch):
    G.on_message(1, "build a login page", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch, None)
    monkeypatch.setattr(G, "_stopped", lambda sid: True)
    G.on_message(1, "use postgres instead", turn=2)
    assert not G.goals(1)["goals"][0].get("updates")


def test_a_dropped_goal_asked_for_again_is_reopened(monkeypatch):
    G.on_message(1, "add a dark mode", turn=1)
    _model(monkeypatch,
           {"goals": [{"id": "g1", "status": "dropped", "text": "add a dark mode"},
                      {"id": None, "status": "open", "text": "fix the login"}],
            "now": ["new"], "current": "fix the login", "question": ""},
           {"goals": [{"id": None, "status": "open", "text": "add a dark mode"}],
            "now": ["new"], "current": "add a dark mode", "question": ""})
    G.on_message(1, "forget the dark mode, fix the login instead", turn=2)
    G.on_message(1, "actually do the dark mode after all", turn=3)
    data = G.goals(1)
    assert data["goals"][0]["status"] == "open" and "g1" in data["now"]


def test_continue_picks_up_the_open_goals(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch, {"goals": [{"id": None, "status": "open", "text": "add csv()"}],
                         "now": ["new"], "current": "add csv()", "question": ""})
    G.on_message(1, "separately add csv()", turn=2)
    G.finish_turn(1, completed=True)                   # csv done, power still open
    out = G.on_message(1, "continue", turn=3)
    assert out["current"] == "add power()" and G.goals(1)["now"] == ["g1"]
    G.finish_turn(1, completed=True)
    assert G.goals(1)["goals"][0]["status"] == "done"


def test_a_model_question_changes_nothing_and_the_answer_sees_the_message(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    seen = _model(monkeypatch,
                  {"goals": [{"id": "g1", "status": "dropped", "text": "add power()"}],
                   "now": [], "current": "", "question": "Drop power()?"},
                  {"goals": [{"id": "g1", "status": "open", "text": "add power()"}],
                   "now": ["g1"], "current": "add power()", "question": ""})
    out = G.on_message(1, "scrap the extra stuff", mid_run=True, turn=1)
    assert out["question"] == "Drop power()?"
    assert G.goals(1)["goals"][0]["status"] == "open"
    G.on_message(1, "no, keep it", turn=2)
    assert "QUESTION WAS ABOUT: scrap the extra stuff" in seen[1]


def test_a_turn_without_the_goal_pass_closes_nothing():
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    G.finish_turn(1, completed=True)                   # e.g. a wake turn
    assert G.goals(1)["goals"][0]["status"] == "open"


def test_short_non_status_orders_do_not_ask(monkeypatch):
    G.on_message(1, "add power()", turn=1)
    G.finish_turn(1, completed=False)
    _model(monkeypatch,
           {"goals": [{"id": None, "status": "open", "text": "add csv()"}],
            "now": ["new"], "current": "add csv()", "question": ""},
           {"goals": [{"id": "g2", "status": "open", "text": "add csv() on port 8080"}],
            "now": ["g2"], "current": "add csv() on port 8080", "question": ""})
    G.on_message(1, "separately add csv()", turn=2)
    out = G.on_message(1, "change that to 8080", mid_run=True, turn=2)
    assert out["question"] == ""


def test_open_goals_are_never_trimmed():
    for i in range(G.MAX_GOALS + 3):
        d = G.goals(1)
        d["goals"].append({"id": f"g{i}", "text": f"t{i}", "status": "open"})
        G._save(1, d)
    G.on_message(1, "continue", turn=1)
    G._trim(G.goals(1))
    assert len(G.goals(1)["goals"]) == G.MAX_GOALS + 3
