"""Goals edited from the progress panel: the user's words are the goal; a
running agent is told at its next step, as the goals now."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_goals as G


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CHAT_GOALS", "1")
    monkeypatch.setattr(G, "_temporary", lambda sid: False)


def _seed(sid=5):
    G._save(sid, {**G._empty(), "seq": 2, "goals": [
        {"id": "g1", "text": "add coupons", "status": "open", "turn": 1},
        {"id": "g2", "text": "write docs", "status": "open", "turn": 1}],
        "now": ["g1", "g2"], "current": "add coupons / write docs"})


def test_edit_drop_done_reopen_and_add():
    _seed()
    out = G.user_edit(5, "g1", text="add coupons with expiry")
    assert out["change"].startswith("changed g1") and "expiry" in out["current"]
    out = G.user_edit(5, "g2", status="dropped")
    assert out["change"].startswith("dropped g2") and out["current"] == "add coupons with expiry"
    assert G.user_edit(5, "g2", status="dropped") is None          # nothing changed
    out = G.user_edit(5, "g1", status="done")
    assert out["change"].startswith("marked done") and out["current"] == ""
    out = G.user_edit(5, "g1", status="open", in_hand=True)
    assert out["current"] == "add coupons with expiry"
    out = G.user_add(5, "add refunds", in_hand=True)
    assert out["id"] == "g3" and "add refunds" in out["current"]
    assert G.user_edit(5, "nope", text="x") is None and G.user_add(5, "  ") is None
    assert G.user_add(5, "Add Refunds") == {"same": True}


def test_idle_chat_reopen_or_add_waits_for_the_next_message():
    _seed()
    G.user_edit(5, "g1", status="done")
    out = G.user_edit(5, "g1", status="open")                 # no run: not in hand
    assert out["current"] == "write docs" and "add coupons" in out["open"][0]
    out = G.user_add(5, "add refunds")
    assert "refunds" not in out["current"]


def test_text_and_status_in_one_edit_both_said_and_bad_status_refused():
    _seed()
    out = G.user_edit(5, "g2", text="write API docs", status="dropped")
    assert len(out["changes"]) == 2 and "changed g2" in out["change"] and "dropped g2" in out["change"]
    with pytest.raises(ValueError):
        G.user_edit(5, "g1", status="paused")


def test_dropping_goal_not_in_hand_keeps_current():
    G._save(5, {**G._empty(), "seq": 2, "goals": [
        {"id": "g1", "text": "add coupons", "status": "open", "turn": 1},
        {"id": "g2", "text": "write docs", "status": "open", "turn": 1}],
        "now": ["g1"], "current": "add coupons, the model's words"})
    out = G.user_edit(5, "g2", status="dropped")
    assert out["current"] == "add coupons, the model's words"


def test_goals_queue_is_apart_from_typed_messages():
    from aiforge_core.runtime import chat_interject as CI
    assert CI.push_goals(77, "note") is False                # no run: nothing queued
    CI.set_steerable(77, True)
    try:
        assert CI.push_goals(77, "note") is False            # a team run / pipeline: no reader
        CI.set_goal_reader(77, True)
        assert CI.push_goals(77, "[GOALS] note") and CI.pending_goals(77)
        assert CI.drain(77) == [] and not CI.pending(77)     # other readers never see it
        assert CI.drain_goals(77) == ["[GOALS] note"] and CI.drain_goals(77) == []
        CI.push_goals(77, "late")
        CI.set_steerable(77, False)                           # the run ended
        assert not CI.pending_goals(77) and not CI.reads_goals(77)
    finally:
        CI.clear(77)


def test_an_edit_off_the_work_in_hand_does_not_stop_it():
    from aiforge_core.api.routes._chat import _overview as O
    G._save(5, {**G._empty(), "seq": 1, "goals": [
        {"id": "g1", "text": "write docs", "status": "open", "turn": 1}], "now": [], "current": ""})
    out = G.user_edit(5, "g1", text="write API docs")
    assert out["emptied"] is False
    note = O._note(out)
    assert "Keep on with the work in hand" in note and "No goal is in hand" not in note


def test_the_route_tells_a_running_agent_as_goals_not_a_message(monkeypatch):
    from aiforge_core.api.routes._chat import _overview as O
    from aiforge_core.runtime import chat_decisions, chat_interject, chat_store
    _seed()
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"id": sid})
    monkeypatch.setattr(chat_interject, "reads_goals", lambda sid: True)
    monkeypatch.setattr(chat_decisions, "record", lambda *a, **k: None)
    monkeypatch.setattr(chat_decisions, "rows", lambda sid: [])
    monkeypatch.setattr(chat_decisions, "current_turn", lambda sid: 1)
    pushed = []
    monkeypatch.setattr(chat_interject, "push_goals", lambda sid, text: pushed.append(text) or True)
    out = O.chat_goal_delete(5, "g2")
    assert out["changed"].startswith("dropped g2") and out["goals"] and out["told_agent"]
    assert pushed and "GOAL NOW (all of it): add coupons" in pushed[0]
    pushed.clear()
    out = O.chat_goal_edit(5, "g1", O._GoalEdit(status="done"))
    assert "No goal is in hand now" in pushed[0]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        O.chat_goal_edit(5, "g1", O._GoalEdit(status="paused"))
    assert e.value.status_code == 400


def test_a_goal_has_one_short_sentence_for_the_panel():
    long = ("Clone the vicom-integration source (C++ repo) from https://gitlab.internal/server/"
            "vicom-integration into the code repo folder, move it to the VM, and modify the "
            "source code to bypass the option check. Note: the executable is scanner-main.")
    assert G.short_title(long) == "Clone the vicom-integration source into the code repo folder"
    assert G.short_title(long, "Fix the scanner-main timeout.") == "Fix the scanner-main timeout"
    assert G.short_title("add coupons") == "add coupons"
    t = G.short_title("Make the login page faster and also fix the flaky test in auth and update docs")
    assert len(t.split()) <= 13 and t.endswith("…")
    data, _ = G._apply({**G._empty()}, {"goals": [{"id": None, "status": "open", "title": "Fix the timeout",
                                                    "text": long}], "now": ["new"]}, "m", 1)
    assert data["goals"][0]["title"] == "Fix the timeout" and data["goals"][0]["text"] == long


def test_short_title_edge_cases():
    assert G.short_title("e.g. add a button") == "e.g. add a button"
    assert G.short_title("Upgrade to v1.2.3 of the lib and rerun tests") == "Upgrade to v1.2.3 of the lib and rerun tests"
    assert G.short_title("Use git@github.com:org/repo.git as the remote, then push") == \
        "Use repo.git as the remote"
    assert G.short_title("Fix this: login fails") == "Fix this: login fails"
    assert G.short_title("Fix the login page: it fails on Safari") == "Fix the login page"
    t = G.short_title(" ".join(["word"] * 400))
    assert t == " ".join(["word"] * 12) + "…"
    assert G.short_title("x", "Clone https://h/r into the folder (C++)") == "Clone r into the folder"


def test_short_title_more_edges():
    assert G.short_title("Clone repo (https://x/y) now") == "Clone repo now"
    assert G.short_title("Deploy to QA. Then run smoke tests") == "Deploy to QA"
    assert G.short_title("Goal: build login") == "Goal: build login"
    assert G.short_title("Use a cache, e.g. Redis") == "Use a cache, e.g. Redis"
