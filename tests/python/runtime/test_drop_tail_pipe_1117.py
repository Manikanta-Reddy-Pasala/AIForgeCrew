"""A trailing `| tail -N` is dropped before a command runs."""
from aiforge_core.runtime import cmd_signals
from aiforge_core.runtime.chat_agent import _shell


def test_tail_suffix_is_dropped_and_noted(monkeypatch):
    monkeypatch.delenv("AIFORGE_CHAT_KEEP_TAIL_PIPE", raising=False)
    seen = []
    monkeypatch.setattr(_shell, "_t_run_command_reuse",
                        lambda a, c: seen.append(a["cmd"]) or {"ok": True, "code": 0})
    r = _shell._t_run_command({"cmd": "cd /r && mvn -o package -DskipTests 2>&1 | tail -50"}, "/r")
    assert seen[-1] == "cd /r && mvn -o package -DskipTests" and "tail" in r["note"]
    _shell._t_run_command({"cmd": "mvn test | tail -n 30"}, "/r")
    assert seen[-1] == "mvn test"
    _shell._t_run_command({"cmd": "mvn test | grep FAIL | tail -5"}, "/r")   # tail after grep: dropped, grep kept
    assert seen[-1] == "mvn test | grep FAIL"
    _shell._t_run_command({"cmd": "ls | head -5"}, "/r")
    assert seen[-1] == "ls | head -5"
    monkeypatch.setenv("AIFORGE_CHAT_KEEP_TAIL_PIPE", "1")
    _shell._t_run_command({"cmd": "mvn test | tail -5"}, "/r")
    assert seen[-1] == "mvn test | tail -5"


def test_hints_never_tell_the_model_to_kill_a_healthy_build():
    h = cmd_signals.job_hint("bg-1", True, None, "mvn test 2>&1 | tail -40")
    assert "command_kill" not in h and "command_wait(id='bg-1', max_s=300)" in h
