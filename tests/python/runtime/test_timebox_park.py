"""An ignored move-on note ends in a harness park: the item goes back to
pending with what still fails, the next unstarted item runs, and the working
context is reset naming it."""
from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _items as I
from aiforge_core.runtime.chat_agent._turn import _timebox as T


def _st(**kw):
    board = {"p1": {"title": "Batches", "status": "done", "from_request": True},
             "p2": {"title": "Coupons", "status": "running", "from_request": True},
             "p3": {"title": "Returns", "status": "pending", "from_request": True},
             "p4": {"title": "Reports", "status": "pending", "from_request": True}}
    base = dict(board=board, readonly_mode=False, builder=None, turn_deadline=None,
                turn_budget_s=None, extensions_used=0, ext_budget=2, item_clock=None,
                time_warned=False, deadline_final=None,
                item_log=[("test", "red", "2 failed: test_coupon_stack")],
                item_start=0, convo=[])
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(I, "active", lambda st: True)
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_TIMEBOX_S", "900")
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_PARK_GRACE_S", "300")


def _run_to(st, until, step=100.0, start=0.0):
    t, notes = start, []
    while t <= until:
        n = T.check(st, now=t)
        if n:
            notes.append((t, n))
        t += step
    return notes


def test_no_park_before_the_grace_runs_out():
    st = _st()
    notes = _run_to(st, 1000.0)
    assert notes and notes[0][0] == 1000.0           # the note at 900+ s
    assert T.park_due(st, now=1000.0) is None
    _run_to(st, 1200.0, start=1100.0)
    assert T.park_due(st, now=1200.0) is None         # 200 s < 300 s grace


def test_ignored_note_parks_the_item_and_starts_the_next():
    st = _st()
    _run_to(st, 1400.0)
    due = T.park_due(st, now=1400.0)
    assert due == ("p2", "p3")
    events = list(I.park(st, *due))
    assert st.board["p2"]["status"] == "pending"
    assert "PARKED" in st.board["p2"]["note"] and "test_coupon_stack" in st.board["p2"]["note"]
    assert st.board["p3"]["status"] == "running"
    assert {"type": "subtask_update", "slug": "p2", "status": "pending"} in events
    # convo too short to reset -> the park line is appended as a directive
    assert any("PARKED" in (m.get("content") or "") for m in st.convo)


def test_an_item_is_parked_once():
    st = _st()
    _run_to(st, 1400.0)
    list(I.park(st, *T.park_due(st, now=1400.0)))
    st.board["p3"]["status"] = "done"
    st.board["p2"]["status"] = "running"              # model came back to p2
    _run_to(st, 5000.0, start=1500.0)
    assert T.park_due(st, now=5000.0) is None


def test_no_park_in_the_last_fifth_of_a_deadline():
    st = _st(turn_deadline=1500.0, turn_budget_s=3000.0)
    _run_to(st, 1400.0)
    assert T.park_due(st, now=1400.0) is None         # 100 s left < 600 s


def test_grace_zero_keeps_it_advisory(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_PARK_GRACE_S", "0")
    st = _st()
    _run_to(st, 3000.0)
    assert T.park_due(st, now=3000.0) is None


def test_the_model_moving_on_itself_means_no_park():
    st = _st()
    _run_to(st, 1100.0)
    st.board["p2"]["status"] = "pending"
    st.board["p3"]["status"] = "running"
    _run_to(st, 2000.0, start=1200.0)
    assert T.park_due(st, now=2000.0) is None
