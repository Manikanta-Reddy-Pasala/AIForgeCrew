"""One board item eating the turn while others never start is told once to
move on (shopkit v6a: parts 4-5 never started, 24 of 60 hidden tests)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _timebox as T


def _st(**kw):
    board = {"p1": {"title": "Batches", "status": "done"},
             "p2": {"title": "Coupons", "status": "running"},
             "p3": {"title": "Returns", "status": "pending"},
             "p4": {"title": "Reports", "status": "pending"}}
    base = dict(board=board, readonly_mode=False, builder=None, turn_deadline=None,
                turn_budget_s=3000, extensions_used=0, ext_budget=2, item_clock=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_an_item_over_its_limit_with_others_not_started_is_told_once():
    st = _st()
    for t in (0.0, 200.0, 400.0, 600.0, 800.0):
        assert T.check(st, now=t) is None              # under 900 s
    note = T.check(st, now=1000.0)
    assert note and "Set p2 back to pending" in note and "mark p3: Returns running" in note
    assert T.check(st, now=2000.0) is None             # once per item


def test_the_limit_follows_closed_items_mean():
    st = _st()
    T.check(st, now=0.0)
    st.item_clock["p1"] = 300.0                         # closed in 5 min
    T.check(st, now=300.0)
    T.check(st, now=600.0)
    assert T.check(st, now=650.0) is not None           # > max(600, 2*300)


def test_moving_on_charges_the_new_item_not_the_old_one():
    st = _st()
    T.check(st, now=0.0)
    T.check(st, now=250.0)
    T.check(st, now=500.0)
    st.board["p3"]["status"] = "running"                # p2 left running
    T.check(st, now=600.0)
    T.check(st, now=800.0)
    assert T.check(st, now=1000.0) is None
    assert round(st.item_clock["p2"]) == 600 and round(st.item_clock["p3"]) == 400


def test_nothing_waiting_or_a_small_board_says_nothing():
    st = _st()
    for s in ("p3", "p4"):
        st.board[s]["status"] = "done"
    for t in range(0, 5001, 250):
        assert T.check(st, now=float(t)) is None
    small = _st(board={"a": {"title": "A", "status": "running"},
                       "b": {"title": "B", "status": "pending"}})
    for t in range(0, 5001, 250):
        assert T.check(small, now=float(t)) is None


def test_the_last_slice_shrinks_the_limit_to_a_fair_share():
    st = _st(turn_deadline=1400.0, extensions_used=2, ext_budget=2)
    T.check(st, now=0.0)
    T.check(st, now=300.0)
    # 3 open items, 1400 s left at start: share ~467 s, limit 1.5x = 700 s
    assert T.check(st, now=600.0) is None
    assert T.check(st, now=650.0) is None
    assert T.check(st, now=760.0) is not None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_ITEM_TIMEBOX_S", "0")
    st = _st()
    for t in range(0, 9001, 250):
        assert T.check(st, now=float(t)) is None


def test_no_move_on_note_once_the_wrap_up_or_final_owns_the_step():
    st = _st(turn_deadline=1400.0, extensions_used=2, ext_budget=2)
    for t in (0.0, 300.0, 600.0, 900.0):
        T.check(st, now=t)
    st.time_warned = True
    assert T.check(st, now=1200.0) is None
    st = _st(turn_deadline=1000.0, extensions_used=2, ext_budget=2)
    for t in (0.0, 300.0, 600.0, 900.0):
        T.check(st, now=t)
    assert T.check(st, now=1100.0) is None             # past the deadline


def test_a_long_wait_between_two_steps_is_not_charged_whole():
    st = _st()
    T.check(st, now=0.0)
    assert T.check(st, now=3600.0) is None             # an hour-long outage
    assert st.item_clock["p2"] == T._STEP_CAP_S


def test_self_added_or_already_boxed_items_are_not_the_next_part():
    st = _st()
    st.board["p3"]["self_added"] = True
    st.board["p4"]["status"] = "done"
    for t in range(0, 1201, 200):
        assert T.check(st, now=float(t)) is None       # only a self-added item waits
    st = _st()
    for t in range(0, 1001, 200):
        note = T.check(st, now=float(t))
    assert note and "mark p3: Returns running" in note
    st.board["p2"]["status"] = "pending"
    st.board["p3"]["status"] = "running"
    for t in range(1200, 2601, 200):
        note = T.check(st, now=float(t))
        assert note is None or "p2:" not in note.split("mark ")[1]


def test_a_state_without_the_fields_gets_them():
    st = _st()
    del st.item_clock
    T.check(st, now=0.0)
    assert st.item_clock == {} and st.item_cur == "p2"
