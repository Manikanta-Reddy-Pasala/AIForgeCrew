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


def test_every_way_of_saving_the_output_is_the_same_test(monkeypatch):
    from aiforge_core.runtime.chat_agent._shell import test_identity
    base = "mvn test -Dtest=ReportStatementBuilderTest"
    for v in (f"cd /r && {base} 2>&1 > /tmp/out.txt; grep 'Tests run:' /tmp/out.txt",
              f"{base} 2>&1 > /tmp/o.txt 2>&1; grep -B2 -A5 Cash /tmp/o.txt",
              f"timeout 300 {base} 2>&1 | tail -40", f"JAVA_HOME=/j {base} && echo ok",
              f"{base} || true"):
        assert test_identity(v) == base, v
    monkeypatch.delenv("AIFORGE_CHAT_STALL_RUNS", raising=False)
    st = _st()
    for i in range(9):
        _limits.note_test_fails(st, 4, test_identity(f"{base} > /tmp/x{i}.txt; grep v{i} /tmp/x{i}.txt"))
    assert _limits._stuck_red(st) is True


def test_a_rarely_run_red_method_does_not_keep_a_stuck_turn_moving(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_STALL_RUNS", raising=False)
    st = _st()
    _limits.note_test_fails(st, 4, "mvn test -Dtest=C")
    for i in range(8):
        cmd = "mvn test -Dtest=C#rare" if i in (2, 5) else "mvn test -Dtest=C"
        _limits.note_test_fails(st, 1 if "#" in cmd else 4, cmd)
    assert _limits._stuck_red(st) is True
