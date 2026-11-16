"""A JVM build that fails on a too-old JDK is re-run once with an installed
JDK that fits, and says how to run commands from then on."""
from aiforge_core.runtime.chat_agent import _shell
from aiforge_core.runtime.chat_agent._turn import _env_guard

ERR = "[ERROR] Fatal error compiling: error: release version 24 not supported"


def test_rerun_with_fitting_jdk(monkeypatch):
    calls = []

    def once(args, cwd):
        calls.append(args["cmd"])
        if "JAVA_HOME" in args["cmd"]:
            return {"ok": True, "code": 0, "stdout": "BUILD SUCCESS", "stderr": ""}
        return {"ok": False, "code": 1, "stdout": ERR, "stderr": ""}
    monkeypatch.setattr(_shell, "_t_run_command_once", once)
    monkeypatch.setattr(_env_guard, "installed_jdks", lambda: [(25, "/j25"), (24, "/j24"), (21, "/j21")])
    r = _shell._t_run_command({"cmd": "cd /r && mvn -o -q test -Dtest=X"}, "/r")
    assert r["ok"] is True and "JAVA_HOME=/j24" in r["toolchain"]
    assert calls == ["cd /r && mvn -o -q test -Dtest=X",
                     "cd /r && JAVA_HOME=/j24 PATH=/j24/bin:$PATH mvn -o -q test -Dtest=X"]


def test_no_rerun_for_other_failures_or_tools(monkeypatch):
    monkeypatch.setattr(_shell, "_t_run_command_once",
                        lambda a, c: {"ok": False, "code": 1, "stdout": "1 failed", "stderr": ""})
    assert "toolchain" not in _shell._t_run_command({"cmd": "mvn test"}, "/r")
    monkeypatch.setattr(_shell, "_t_run_command_once",
                        lambda a, c: {"ok": False, "code": 1, "stdout": ERR, "stderr": ""})
    assert "toolchain" not in _shell._t_run_command({"cmd": "make build"}, "/r")
