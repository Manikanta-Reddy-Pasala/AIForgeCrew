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
    assert "FAILED" in res["warning"] and board["a"]["note"].startswith("UNVERIFIED")


def test_a_done_verdict_this_finish_skips_reminders_for_the_request_parts():
    from aiforge_core.runtime.chat_agent._turn import _tasks
    board = {"part-1": {"title": "a", "status": "done", "from_request": True},
             "part-2": {"title": "b", "status": "pending", "from_request": True},
             "mine": {"title": "c", "status": "pending"}}
    assert _tasks.open_planned(board) == ["mine"]


def test_an_earlier_done_verdict_is_cleared_when_the_check_does_not_run_again():
    from aiforge_core.runtime.chat_agent._turn import _done_check
    st = SimpleNamespace(goal="Do it.", done_check_said="done", done_check_unfinished=False)
    g = _done_check.gate(st, {"text": ""})
    try:
        while True:
            next(g)
    except StopIteration:
        pass
    assert st.done_check_said is None      # a later finish never reuses it


def test_parts_close_unless_the_check_ran_without_saying_done():
    from aiforge_core.runtime.chat_agent._turn import _done_check
    st = SimpleNamespace()                                   # check never ran
    assert _done_check.parts_may_close(st)
    st.done_check_ran, st.done_check_said = True, ""          # timed out
    assert not _done_check.parts_may_close(st)
    st.done_check_said = "done"
    assert _done_check.parts_may_close(st)
    st2 = SimpleNamespace(contract_checked=9, contract_gaps=[])   # counts are no check
    assert not _done_check.judged_done(st2)
