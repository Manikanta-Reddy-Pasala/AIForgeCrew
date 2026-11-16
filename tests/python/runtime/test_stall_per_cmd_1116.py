"""A test command stuck red stops extensions, even the first one."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _limits


def _st():
    return SimpleNamespace(granted_at_step=None, extensions_used=0, ext_budget=2,
                           wt_fp0=None, cwd=None, reads_new=1, edits_made=1,
                           progress_mark=None, spin_events=0)


def test_class_stuck_red_refuses_first_extension(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_STALL_RUNS", raising=False)
    st = _st()
    for f in (3, 2, 1):
        _limits.note_test_fails(st, f, "mvn test -Dtest=CashflowTest")
    _limits.note_test_fails(st, 0, "mvn test -Dtest=CashflowTest#one")   # green method
    assert _limits._may_extend(st, 10) is True                           # still moving
    st.granted_at_step = None; st.extensions_used = 0; st.edits_made = 2
    for _ in range(8):
        _limits.note_test_fails(st, 1, "mvn test -Dtest=CashflowTest")
    assert _limits._stuck_red(st) is True
    assert _limits._may_extend(st, 20) is False


def test_improving_or_green_is_not_stuck(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_STALL_RUNS", raising=False)
    st = _st()
    for f in (5, 5, 5, 5, 5, 5, 5, 4):                 # a new best resets the count
        _limits.note_test_fails(st, f, "pytest -q")
    assert _limits._stuck_red(st) is False
    st2 = _st()
    for _ in range(12):
        _limits.note_test_fails(st2, 0, "cargo test")  # green: never stuck
    assert _limits._stuck_red(st2) is False


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_STALL_RUNS", "0")
    st = _st()
    for _ in range(20):
        _limits.note_test_fails(st, 1, "mvn test")
    assert _limits._stuck_red(st) is False
