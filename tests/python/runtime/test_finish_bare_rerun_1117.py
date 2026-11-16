"""The finish check re-runs the turn's test command bare, so the runner's
own counts and exit code decide."""
from types import SimpleNamespace

from aiforge_core.runtime.chat_agent._turn import _outcomes as O


def test_bare_command_keeps_cd_and_env_and_cuts_the_tail():
    c = ("cd /r && JAVA_HOME=/j PATH=/j/bin:$PATH mvn -o test 2>&1 > /tmp/o.txt; "
         "grep BUILD /tmp/o.txt")
    assert O._bare_test_command(c) == "cd /r && JAVA_HOME=/j PATH=/j/bin:$PATH mvn -o test"
    assert O._bare_test_command("mvn -o -q test -Dtest=X | tail -40") == "mvn -o -q test -Dtest=X"
    assert O._bare_test_command("pytest -q tests/test_a.py") == "pytest -q tests/test_a.py"


def test_rerun_uses_the_bare_command(monkeypatch):
    seen = []
    from aiforge_core.runtime.chat_agent._turn import _done_watch
    monkeypatch.setattr(_done_watch, "_run", lambda cmd, cwd, timeout=0: seen.append(cmd) or (
        1, "[ERROR] Tests run: 1038, Failures: 1, Errors: 0, Skipped: 32"))
    st = SimpleNamespace(cwd="/r", last_test_cmd=("run_command", {
        "cmd": "cd /r && mvn -o test 2>&1 > /tmp/o.txt; grep BUILD /tmp/o.txt"}))
    ok, out = O._rerun_own_test(st, "/r")
    assert seen == ["cd /r && mvn -o test"] and ok is False


def test_no_counts_after_running_tests_is_no_verdict_not_a_compile_pass(monkeypatch):
    monkeypatch.setattr(O, "_rerun_own_test", lambda st, cwd: None)
    monkeypatch.setattr(O, "_python_project", lambda cwd: False)
    monkeypatch.setattr(O, "_project_python", lambda st, cwd: None)
    monkeypatch.setattr(O, "_compile_check", lambda st, cwd: (True, "BUILD SUCCESS"))
    st = SimpleNamespace(cwd="/r", last_test_cmd=("run_command", {"cmd": "mvn -o test"}))
    assert O._project_verify(st, "/r") == (None, "")
    st2 = SimpleNamespace(cwd="/r")
    assert O._project_verify(st2, "/r") == (True, "BUILD SUCCESS")       # never tested: compile
