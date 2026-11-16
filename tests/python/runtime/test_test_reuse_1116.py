"""A test command re-run on unchanged code returns the last result at once."""
from aiforge_core.runtime.chat_agent import _shell
from aiforge_core.runtime.chat_agent._turn import _outcomes


def test_reuse_on_unchanged_code(monkeypatch):
    runs = []
    monkeypatch.setattr(_shell, "_TEST_RUNS", {})
    monkeypatch.setattr(_shell, "_jdk_key", lambda cwd: "/r")
    fp = {"v": "A"}
    monkeypatch.setattr(_outcomes, "content_fingerprint", lambda root: fp["v"])
    monkeypatch.setattr(_shell, "_t_run_command_jdk",
                        lambda a, c: runs.append(a["cmd"]) or {"ok": False, "code": 1, "stdout": "1 failed"})
    _shell._t_run_command({"cmd": "cd /r && timeout 300 mvn -o -q test -Dtest=X 2>&1 | tail -40"}, "/r")
    r = _shell._t_run_command({"cmd": "mvn -o -q test -Dtest=X 2>&1 | grep FAIL"}, "/r")
    assert len(runs) == 1 and "reused" in r and r["stdout"] == "1 failed"
    _shell._t_run_command({"cmd": "mvn -o -q test -Dtest=Y"}, "/r")          # other test: runs
    fp["v"] = "B"                                                            # code changed: runs
    _shell._t_run_command({"cmd": "mvn -o -q test -Dtest=X"}, "/r")
    assert len(runs) == 3


def test_non_test_commands_are_never_reused(monkeypatch):
    runs = []
    monkeypatch.setattr(_shell, "_TEST_RUNS", {})
    monkeypatch.setattr(_shell, "_t_run_command_jdk", lambda a, c: runs.append(1) or {"ok": True, "code": 0})
    _shell._t_run_command({"cmd": "ls -la"}, "/r")
    _shell._t_run_command({"cmd": "ls -la"}, "/r")
    assert len(runs) == 2


def test_off_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_TEST_REUSE", "0")
    assert _shell._test_reuse("mvn test", "/r") == (None, None, None)
