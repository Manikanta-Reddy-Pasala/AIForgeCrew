"""The harness sees the work is done instead of waiting for the model: big
benchmark runs never replied without a tool call and used all 50 minutes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiforge_core.runtime.chat_agent._turn import _done_watch as W
from aiforge_core.runtime.chat_agent._turn import _outcomes


def _drain(g):
    evs = []
    try:
        while True:
            evs.append(next(g))
    except StopIteration as e:
        return evs, e.value


@pytest.fixture
def st(tmp_path, monkeypatch):
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "m.py").write_text(
        "class Cart:\n    def __init__(self):\n        self.items = []\n\n"
        "    def total(self, *, tax=0):\n        return 1\n")
    monkeypatch.setattr(_outcomes, "content_fingerprint", lambda cwd: "fp1")
    return SimpleNamespace(
        goal="Add `Cart.total(self, *, tax=0)` and `Cart.items` in `shop/m.py`.",
        cwd=str(tmp_path), convo=[{"role": "system", "content": "s"}], edits_made=3,
        green_at_edits=3, last_green_fp="fp1", steers=[], session_id=None,
        board={"part-1": {"title": "totals", "status": "running"}})


def test_done_work_gets_one_note_then_the_turn_ends(st):
    evs, sig = _drain(W.watch(st, 10))
    assert sig is None and st.board["part-1"]["status"] == "done"
    assert "Done check" in st.convo[-1]["content"]
    for n in range(11, 10 + W.grace()):
        assert _drain(W.watch(st, n))[1] is None
    evs, sig = _drain(W.watch(st, 10 + W.grace()))
    assert sig == "return"
    assert any(e.get("type") == "message" and "full test suite passes" in e["text"]
               for e in evs)


def test_an_edit_after_the_note_means_not_done(st):
    _drain(W.watch(st, 10))
    st.edits_made = 4                      # edited: suite not green on this code
    assert _drain(W.watch(st, 20))[1] is None and st.done_noted_at is None


def test_not_done_without_a_green_suite_or_with_gaps(st, tmp_path):
    st.last_green_fp = None
    assert not W.done_now(st)
    st.last_green_fp = "fp1"
    (tmp_path / "shop" / "m.py").write_text("class Cart:\n    pass\n")
    assert not W.done_now(st)              # Cart.total missing


def test_few_names_need_no_untouched_item(st):
    st.goal = "make it faster"
    assert W.done_now(st)                       # running item: may be finished
    st.board["part-2"] = {"title": "docs", "status": "pending"}
    assert not W.done_now(st)                   # never started


def test_read_only_and_off_switch(st, monkeypatch):
    st.readonly_mode = True
    assert not W.done_now(st)
    st.readonly_mode = False
    monkeypatch.setenv("AIFORGE_CHAT_DONE_WATCH", "0")
    assert not W.done_now(st)


def test_a_user_message_resets_the_watch(st):
    _drain(W.watch(st, 10))
    st.steers.append("also add coupons")
    _drain(W.watch(st, 10 + W.grace()))
    assert st.done_noted_at == 10 + W.grace()   # noted afresh, not ended


def test_a_failure_the_request_names_counts_as_green():
    st = SimpleNamespace(goal="(`tests/x.py::T::test_lazy_load` already fails; ignore it)")
    out = {"ok": False, "stdout": "FAILED tests/x.py::T::test_lazy_load - x\n1 failed, 9 passed\n"}
    assert _outcomes._only_known_failures(st, out)
    out2 = {"ok": False, "stdout": "FAILED tests/x.py::T::test_ddl - x\n1 failed, 9 passed\n"}
    assert not _outcomes._only_known_failures(st, out2)


def test_the_harness_runs_the_tests_after_idle_steps(st, monkeypatch):
    from aiforge_core.runtime.chat_agent import _context
    st.last_green_fp = None
    calls = []
    monkeypatch.setattr(_context, "_run_project_verify",
                        lambda cwd: calls.append(cwd) or (True, "5 passed"))
    for n in range(1, W.IDLE + 2):
        _drain(W.watch(st, n))
    assert len(calls) == 1 and st.last_green_fp == "fp1"
    assert st.done_noted_at is not None          # and the note went out
    for n in range(W.IDLE + 2, W.IDLE + 5):
        _drain(W.watch(st, n))
    assert len(calls) == 1                        # once per code state
