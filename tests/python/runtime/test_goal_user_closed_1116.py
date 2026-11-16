"""A goal the user deleted or ticked done in the progress panel does not come
back when they type the next message (unless that message is about it)."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import chat_goals as G


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_GOALS", "1")
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(G, "_temporary", lambda sid: False)
    G._MEM.clear()
    G._SESSION_LOCKS.clear()


def _model(monkeypatch, reply):
    monkeypatch.setattr(G, "_ask", lambda role, user, sid: json.dumps(reply))


def _seed():
    G._save(7, {**G._empty(), "seq": 2, "goals": [
        {"id": "g1", "text": "add coupon codes to checkout", "title": "Add coupon codes",
         "status": "open", "turn": 1},
        {"id": "g2", "text": "write the API docs", "status": "open", "turn": 1}],
        "now": ["g1", "g2"], "current": "x"})


def _status():
    return {g["id"]: g["status"] for g in G.goals(7)["goals"]}


@pytest.mark.parametrize("how", ["dropped", "done"])
def test_closed_goal_is_not_reopened_by_id_on_an_unrelated_message(monkeypatch, how):
    _seed()
    G.user_edit(7, "g1", status=how)
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open", "text": "add coupon codes to checkout"},
                                   {"id": None, "status": "open", "text": "fix the login page"}],
                         "now": ["g1", "new"], "current": "", "question": ""})
    G.on_message(7, "now fix the login page", turn=2)
    st = _status()
    assert st["g1"] == how and "g3" in st and len(st) == 3


def test_closed_goal_is_not_added_again_as_a_copy(monkeypatch):
    _seed()
    G.user_edit(7, "g1", status="dropped")
    _model(monkeypatch, {"goals": [{"id": None, "status": "open", "text": "Add coupon codes to the checkout"}],
                         "now": ["new"], "current": "", "question": ""})
    G.on_message(7, "thanks, looks fine", turn=2)
    texts = [g["text"] for g in G.goals(7)["goals"] if g["status"] == "open"]
    assert not any("coupon" in t.lower() for t in texts)


def test_a_message_about_the_closed_goal_may_reopen_it(monkeypatch):
    _seed()
    G.user_edit(7, "g1", status="done")
    _model(monkeypatch, {"goals": [{"id": "g1", "status": "open",
                                    "text": "add coupon codes to checkout, also expiry"}],
                         "now": ["g1"], "current": "", "question": ""})
    G.on_message(7, "the coupon codes in checkout still fail, add expiry", turn=2)
    assert _status()["g1"] == "open"


def test_reopening_from_the_panel_clears_the_mark():
    _seed()
    G.user_edit(7, "g1", status="dropped")
    G.user_edit(7, "g1", status="open")
    assert "closed_by_user" not in G.goals(7)["goals"][0]
