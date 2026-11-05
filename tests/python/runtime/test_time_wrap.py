"""Out of time, a turn ends with a summary instead of being cut mid-edit:
one wrap-up note in the last slice, then one forced final answer."""
from __future__ import annotations

import time
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent import _native
from aiforge_core.runtime.chat_agent._turn import _limits as L


def _drain(g):
    out = []
    try:
        while True:
            out.append(next(g))
    except StopIteration as e:
        return out, e.value


def _st(left, ext_used=2, ext_budget=2):
    return SimpleNamespace(turn_deadline=time.monotonic() + left, turn_budget_s=1000,
                           extensions_used=ext_used, ext_budget=ext_budget,
                           convo=[{"role": "user", "content": "OBSERVATION: x"}],
                           session_id=77, cwd="/tmp")


def test_the_last_slice_gets_one_wrap_up_note():
    st = _st(left=100)
    evs, sig = _drain(L._deadline_guard(st, 5))
    assert sig is None and "Finish the change you are on" in st.convo[-1]["content"]
    _drain(L._deadline_guard(st, 6))
    assert st.convo[-1]["content"].count("Finish the change you are on") == 1


def test_no_wrap_up_while_an_extension_can_still_follow():
    st = _st(left=100, ext_used=0)
    _drain(L._deadline_guard(st, 5))
    assert "Finish the change" not in st.convo[-1]["content"]


def test_out_of_time_forces_one_final_answer_then_stops():
    st = _st(left=-1)
    evs, sig = _drain(L._deadline_guard(st, 5))
    assert sig is None and 77 in _native._FORCE_FINAL and st.deadline_final == 5
    _native._FORCE_FINAL.discard(77)
    st.granted_at_step, st.progress_mark, st.reads_new, st.edits_made = None, None, 0, 0
    st.wt_fp0 = ""
    evs, sig = _drain(L._deadline_guard(st, 6))             # one step later: the stop
    assert sig == "return"


def test_the_forced_final_answer_skips_the_finish_checks(monkeypatch):
    """Time is up: a send-back would only be cut off one step later, and the
    summary would be lost to the plain timeout message."""
    from aiforge_core.runtime.chat_agent._turn import _finish
    called = []
    def checks(*a, **k):
        called.append(1)
        yield from ()
        return "continue"
    def accept(*a, **k):
        yield {"type": "message", "text": "summary"}
        return "return"
    monkeypatch.setattr(_finish, "_finish_checks", checks)
    monkeypatch.setattr(_finish, "_accept_final", accept)
    st = SimpleNamespace(deadline_final=5)
    evs, sig = _drain(_finish._handle_final(st, {"text": "x"}, None, False, False, False,
                                            "/tmp", [], ""))
    assert sig == "return" and not called and evs[-1]["text"] == "summary"
