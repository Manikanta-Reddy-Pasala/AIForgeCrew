"""Board timebox: advisory move-on after a request part has closed."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _timebox as T
from aiforge_core.runtime.chat_agent import _native


def _board(*titles, statuses=None, from_request=True):
    statuses = statuses or ["pending"] * len(titles)
    return {
        f"part-{i+1}": {
            "title": t, "status": statuses[i], "from_request": from_request,
        }
        for i, t in enumerate(titles)
    }


def _st(board, **kw):
    base = dict(
        board=board, item_clock={}, item_clock_at=0.0, item_boxed=set(),
        item_running=set(), item_cur=None, turn_deadline=None,
        extensions_used=0, ext_budget=0, time_warned=False,
        deadline_final=None, edits_made=0, session_id=99,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_timebox_skips_until_a_request_part_is_closed(monkeypatch):
    """boc*: parking Tokens before any close left part-1 at 0/12."""
    monkeypatch.setattr(T, "_first_limit", lambda: 60.0)
    monkeypatch.setattr(T, "_FLOOR_S", 60.0)
    from aiforge_core.runtime.chat_agent._turn import _items as I
    monkeypatch.setattr(I, "active", lambda st: True)
    board = _board("Tokens", "Exprs", "Parser",
                   statuses=["running", "pending", "pending"])
    st = _st(board, item_cur="part-1", item_running={"part-1"},
             item_clock={"part-1": 120.0}, item_clock_at=1000.0)
    assert T.check(st, now=1001.0) is None
    assert st.board["part-1"]["status"] == "running"


def test_timebox_advises_move_on_after_a_request_part_closes(monkeypatch):
    """Advisory only — do not park/force (g2a 17/25 vs boc hard-move 2–7)."""
    monkeypatch.setattr(T, "_first_limit", lambda: 60.0)
    monkeypatch.setattr(T, "_FLOOR_S", 60.0)
    from aiforge_core.runtime.chat_agent._turn import _items as I
    monkeypatch.setattr(I, "active", lambda st: True)
    board = _board("Tokens", "Exprs", "Parser",
                   statuses=["done", "running", "pending"])
    st = _st(board, item_cur="part-2", item_running={"part-2"},
             item_clock={"part-2": 120.0}, item_clock_at=1000.0)
    _native.clear_force_write(99)
    note = T.check(st, now=1001.0)
    assert note and "part-3" in note
    assert st.board["part-2"]["status"] == "running"  # not parked
    assert 99 not in _native._FORCE_WRITE
    _native.clear_force_write(99)


def test_request_closed_helper():
    board = _board("Tokens", "Exprs", statuses=["pending", "pending"])
    assert not T._request_closed(_st(board))
    board["part-1"]["status"] = "done"
    assert T._request_closed(_st(board))
