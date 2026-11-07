"""The finish check's red verdict yields to the turn's own full test
command when every failure is one the original code has too (uc-feature:
3 failures already in the repo cost two fix rounds and 4 of 6 minutes)."""
from __future__ import annotations

from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _done_watch as DW
from aiforge_core.runtime.chat_agent._turn import _outcomes as O


def _st(tmp_path, **kw):
    return SimpleNamespace(cwd=str(tmp_path), edits_made=2, head0="abc123",
                           last_full_test=(1, {"cmd": "python -m pytest tests -q"}), **kw)


def test_red_only_for_pre_existing_failures_counts_as_green(monkeypatch, tmp_path):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    monkeypatch.setattr(O, "_whole_suite", lambda cmd, base="": True)
    st = _st(tmp_path)
    monkeypatch.setattr(O, "_only_known_failures", lambda st, r: False)
    calls = []
    monkeypatch.setattr(DW, "own_test_verdict", lambda st: calls.append(1) or (True, "3 failed (old)"))
    assert O._verify_known_ok(st, False, "FAILED tests/t.py::a") == (True, "3 failed (old)")
    O._verify_known_ok(st, False, "FAILED tests/t.py::a")
    assert len(calls) == 1                              # once per code state


def test_new_failures_stay_red(monkeypatch, tmp_path):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    monkeypatch.setattr(O, "_whole_suite", lambda cmd, base="": True)
    st = _st(tmp_path)
    monkeypatch.setattr(O, "_only_known_failures", lambda st, r: False)
    monkeypatch.setattr(DW, "own_test_verdict", lambda st: (False, "1 failed"))
    assert O._verify_known_ok(st, False, "FAILED tests/t.py::b") == (False, "FAILED tests/t.py::b")
    monkeypatch.setattr(DW, "own_test_verdict", lambda st: None)       # no own command
    st2 = _st(tmp_path, )
    st2.edits_made = 3
    assert O._verify_known_ok(st2, False, "x")[0] is False


def test_a_narrow_command_or_no_base_never_vouches(monkeypatch, tmp_path):
    from aiforge_core.runtime import shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    monkeypatch.setattr(O, "_only_known_failures", lambda st, r: False)
    monkeypatch.setattr(DW, "own_test_verdict", lambda st: (True, "green"))
    monkeypatch.setattr(O, "_whole_suite", lambda cmd, base="": False)
    assert O._verify_known_ok(_st(tmp_path), False, "FAILED t::a")[0] is False
    monkeypatch.setattr(O, "_whole_suite", lambda cmd, base="": True)
    st = _st(tmp_path)
    st.head0 = None
    assert O._verify_known_ok(st, False, "FAILED t::a")[0] is False


def test_failing_ids_checked_on_the_base_when_no_whole_suite_command(monkeypatch, tmp_path):
    """uc-feature: the turn only ran tests/test_helper.py."""
    from aiforge_core.runtime import git_target, shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    monkeypatch.setattr(git_target, "command_base", lambda cwd: cwd)
    monkeypatch.setattr(O, "_only_known_failures", lambda st, r: False)
    seen = {}
    monkeypatch.setattr(DW, "pre_existing", lambda here, cmd, out, base, only_failing=False: seen.update(cmd=cmd) or True)
    st = SimpleNamespace(cwd=str(tmp_path), edits_made=2, head0="abc",
                         last_test_cmd=(1, {"cmd": "/v/bin/python -m pytest tests/test_helper.py"}))
    out = "FAILED tests/test_a.py::test_x - boom\nFAILED tests/test_b.py::test_y\n2 failed"
    assert O._verify_known_ok(st, False, out) == (True, out)
    assert "/v/bin/python -m pytest" in seen["cmd"] and "tests/test_a.py::test_x" in seen["cmd"]
    monkeypatch.setattr(DW, "pre_existing", lambda *a: False)
    st2 = SimpleNamespace(cwd=str(tmp_path), edits_made=3, head0="abc",
                          last_test_cmd=(1, {"cmd": "/v/bin/python -m pytest tests/test_helper.py"}))
    assert O._verify_known_ok(st2, False, out)[0] is False


def test_unlisted_failures_or_collection_errors_never_count_as_old(monkeypatch, tmp_path):
    from aiforge_core.runtime import git_target, shell_writes
    monkeypatch.setattr(shell_writes, "command_of", lambda x: x.get("cmd"))
    monkeypatch.setattr(git_target, "command_base", lambda cwd: cwd)
    monkeypatch.setattr(DW, "pre_existing", lambda *a, **k: True)

    def st():
        return SimpleNamespace(cwd=str(tmp_path), edits_made=2, head0="abc",
                               last_test_cmd=(1, {"cmd": "/v/bin/python -m pytest t.py"}))
    assert O._ids_green(st(), "FAILED a.py::x\n3 failed in 1s") is None          # 2 unlisted
    assert O._ids_green(st(), "FAILED a.py::x\nERROR b.py - ImportError\n1 failed, 1 error") is None
    s2 = st()
    s2.own_red_at = "x"
    assert O._ids_green(s2, "FAILED a.py::x\n1 failed in 1s") is None              # own suite red
    assert O._ids_green(st(), "FAILED a.py::x\n1 failed in 1s") is not None
