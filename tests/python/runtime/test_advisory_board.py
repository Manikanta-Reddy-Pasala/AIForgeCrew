"""The board's gates advise instead of refusing, and the finish gates share
one send-back budget (OpenCode/Codex/OpenHands end on the first text reply)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _finish, _section_check


def test_a_section_close_stands_and_carries_the_section_text(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_BOARD_STRICT", raising=False)
    goal = "Build it.\n\n## 1. Totals\nAdd `Cart.total(self, *, tax=0)`.\n\n## 2. Tax\nAdd tax.\n"
    st = SimpleNamespace(goal=goal, board={"part-1": {"title": "1. Totals", "status": "done",
                                                      "from_request": True}})
    res, ev = _section_check.check_back(st, {"ok": True, "slug": "part-1", "status": "done"},
                                        [], {"part-1": "running"})
    assert res["ok"] is True and st.board["part-1"]["status"] == "done"
    assert "Cart.total" in res["section"] and "Marked done" in res["check"]


def test_the_finish_gates_share_two_send_backs_that_refill_after_real_work(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_FINISH_SENDBACKS", raising=False)
    st = SimpleNamespace(edits_made=1)
    assert _finish._budget_left(st, "contract_nudges", 9)
    assert _finish._budget_left(st, "done_check_nudges", 9)
    assert not _finish._budget_left(st, "contract_nudges", 9)    # shared: spent
    for own in ("board_nudges", "verify_rounds", "intent_nudges", "toolless_retries"):
        assert _finish._budget_left(st, own, 9), own              # keep their own bounds
    st.edits_made = 1 + _finish._REFILL_EDITS                    # real work since
    assert _finish._budget_left(st, "done_check_nudges", 9)


def test_the_advisory_item_close_is_accepted_with_a_warning(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._turn import _items
    monkeypatch.delenv("AIFORGE_CHAT_BOARD_STRICT", raising=False)
    board = {s: {"title": s, "status": "running"} for s in ("a", "b", "c")}
    board["a"]["status"] = "done"
    st = SimpleNamespace(board=board, cwd=str(tmp_path), readonly_mode=False, builder=None,
                         item_log=[], item_seen={}, item_start=0, item_prev_start=None,
                         item_rejects={}, item_reset_pending=False, item_resets=0,
                         item_last_closed=None, item_fresh_close=False, file_hashes={},
                         tree_hashes={}, tree_pending=False)
    res, ev = _items.review_progress(st, {"ok": True, "slug": "a", "status": "done"}, [])
    assert res["ok"] is True and board["a"]["status"] == "done"
    assert "accepted as done" in res.get("warning", "")


def test_an_item_whose_last_test_run_is_red_is_accepted_unverified_and_named(monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._turn import _items
    monkeypatch.delenv("AIFORGE_CHAT_BOARD_STRICT", raising=False)
    board = {s: {"title": s, "status": "running"} for s in ("a", "b", "c")}
    board["a"]["status"] = "done"
    st = SimpleNamespace(board=board, cwd=str(tmp_path), readonly_mode=False, builder=None,
                         item_log=[("test", "red", "1 failed; failing: t.py::x")], item_seen={},
                         item_start=0, item_prev_start=None, item_rejects={},
                         item_reset_pending=False, item_resets=0, item_last_closed=None,
                         item_fresh_close=False, file_hashes={}, tree_hashes={}, tree_pending=False)
    res, ev = _items.review_progress(st, {"ok": True, "slug": "a", "status": "done"}, [])
    assert res["ok"] is True and board["a"]["status"] == "done"
    assert "FAILED" in res["warning"] and "UNVERIFIED" in board["a"]["note"]
