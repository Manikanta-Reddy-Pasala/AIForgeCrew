"""A running command is checked on, not blindly waited for: run_command hands
a still-running command back at the check-in, returns at once when its output
shows an error or a prompt, and command_wait / command_output / command_kill
let the agent look again, stop it, or keep waiting — returning early on an
exit, an error, a prompt or a stall.

Real subprocesses throughout: a slow printer, one that errors while it keeps
running, one sitting on a prompt, and a silent CPU-busy one."""
from __future__ import annotations

import os
import time

import pytest

from aiforge_core.runtime import cmd_idle, cmd_jobs
from aiforge_core.runtime.chat_agent import _cmd_tools as T
from aiforge_core.runtime.chat_agent import _shell as S


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_BG_DB_PATH", str(tmp_path / "bg.db"))
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    monkeypatch.setenv("AIFORGE_CMD_STUCK_CHECK_S", "45")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    for k in ("AIFORGE_CHAT_CMD_TIMEOUT_S",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(S, "_workspace_root", lambda: None)
    turn = cmd_jobs.begin_turn()
    yield tmp_path
    cmd_jobs.end_turn(turn)


def _run(cmd, cwd, **kw):
    return S._t_run_command({"cmd": cmd, **kw}, str(cwd))


def test_a_fast_command_returns_as_before(env):
    res = _run("echo hi", env)
    assert res == {"ok": True, "code": 0, "stdout": "hi\n", "stderr": ""}


def test_a_slow_printer_is_handed_back_then_waited_to_exit(env):
    t0 = time.monotonic()
    res = _run("for i in 1 2 3 4 5 6 7 8; do echo line $i; sleep 0.4; done", env)
    assert time.monotonic() - t0 < 3
    assert res["running"] is True and res["id"].startswith("bg-")
    assert "line 1" in res["new_output"]
    assert "command_wait" in res["hint"]
    done = T._t_command_wait({"id": res["id"], "max_s": 20}, str(env))
    assert done["running"] is False and done["code"] == 0
    assert "line 8" in done["new_output"]
    assert "line 1" not in done["new_output"]       # only what is new


def test_an_error_while_it_keeps_running_returns_at_once(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "30")
    t0 = time.monotonic()
    res = _run("echo start; sleep 1; echo 'Traceback (most recent call last):';"
               " echo 'ValueError: boom'; sleep 30", env)
    assert time.monotonic() - t0 < 6
    assert res["running"] is True
    assert "traceback" in res["returned_because"]
    killed = T._t_command_kill({"id": res["id"]}, str(env))
    assert killed["killed"] is True and killed["running"] is False


def test_a_prompt_waiting_for_input_returns_at_once(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "30")
    t0 = time.monotonic()
    res = _run("printf 'Overwrite existing config? [y/N] '; sleep 30", env)
    assert time.monotonic() - t0 < 6
    assert "waiting for input" in res["returned_because"]
    T._t_command_kill({"id": res["id"]}, str(env))


def test_a_stuck_command_is_reported_not_killed(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_STUCK_CHECK_S", "1")
    res = _run("echo ready; sleep 30", env)
    t0 = time.monotonic()
    got = T._t_command_wait({"id": res["id"], "max_s": 60}, str(env))
    assert time.monotonic() - t0 < 10
    assert got["stuck"] is True and got["running"] is True
    assert "looks stuck" in got["returned_because"]
    T._t_command_kill({"id": res["id"]}, str(env))


def test_a_silent_cpu_busy_command_is_not_called_stuck(env, monkeypatch):
    if cmd_idle.group_cpu_s(os.getpgid(0)) is None:
        pytest.skip("no CPU signal on this platform")
    monkeypatch.setenv("AIFORGE_CMD_STUCK_CHECK_S", "1")
    res = _run("python3 -c \"import time\nt=time.time()\n"
               "while time.time()-t<4: pass\nprint('built')\"", env)
    assert res["running"] is True
    got = T._t_command_wait({"id": res["id"], "max_s": 20}, str(env))
    assert got.get("stuck") is None
    assert got["running"] is False and "built" in got["new_output"]


def test_a_peek_does_not_wait(env):
    res = _run("echo a; sleep 0.3; echo b; sleep 30", env)
    t0 = time.monotonic()
    peek = T._t_command_output({"id": res["id"]}, str(env))
    assert time.monotonic() - t0 < 1
    assert peek["running"] is True
    T._t_command_kill({"id": res["id"]}, str(env))


def test_the_healthy_wait_doubles_and_a_signal_resets_it(env):
    res = _run("while true; do echo tick; sleep 0.2; done", env)
    job = cmd_jobs.find(res["id"])
    assert cmd_jobs.default_wait_s(job) == 15
    got = cmd_jobs.wait(job, 0.5)
    assert got["running"] is True and "still working" in got["returned_because"]
    assert cmd_jobs.default_wait_s(job) == 30
    job.streak = 10
    assert cmd_jobs.default_wait_s(job) == 300
    T._t_command_kill({"id": res["id"]}, str(env))


def test_the_loop_key_follows_the_jobs_progress(env):
    res = _run("while true; do echo tick; sleep 0.2; done", env)
    a = T.progress_sig("command_wait", {"id": res["id"]})
    time.sleep(0.6)
    b = T.progress_sig("command_wait", {"id": res["id"]})
    assert a and a != b                          # working: a new call each time
    T._t_command_kill({"id": res["id"]}, str(env))
    quiet = _run("echo once; sleep 30", env)
    time.sleep(0.3)
    c = T.progress_sig("command_wait", {"id": quiet["id"]})
    time.sleep(0.5)
    assert c == T.progress_sig("command_wait", {"id": quiet["id"]})
    assert T.progress_sig("run_command", {"cmd": "x"}) == ""
    T._t_command_kill({"id": quiet["id"]}, str(env))


def test_the_turn_ending_kills_its_handed_off_commands_only(env):
    turn = cmd_jobs.begin_turn()
    handed = _run("echo x; sleep 30", env)
    bg = _run("sleep 30", env, background=True)
    assert bg["id"].startswith("bg-")
    h, b = cmd_jobs.find(handed["id"]), cmd_jobs.find(bg["id"])
    assert cmd_jobs.end_turn(turn) == 1
    assert not h.alive()
    assert b.alive()                              # explicit background stays
    T._t_command_kill({"id": bg["id"]}, str(env))
    assert not b.alive()


def test_background_jobs_can_be_peeked(env):
    bg = _run("echo started; sleep 30", env, background=True)
    time.sleep(0.5)
    peek = T._t_command_output({"id": bg["id"]}, str(env))
    assert "started" in peek["new_output"]
    T._t_command_kill({"id": bg["id"]}, str(env))


def test_an_unknown_id_says_what_is_running(env):
    res = T._t_command_wait({"id": "bg-999999"}, str(env))
    assert res["ok"] is False and "running" in res


def test_checkins_off_keeps_the_old_blocking_run(env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "0")
    res = _run("sleep 1.5; echo done", env)
    assert res["ok"] is True and "done" in res["stdout"]


def test_signals():
    from aiforge_core.runtime import cmd_signals as sig
    assert sig.failure_in("ok\nnpm ERR! missing script") .startswith("npm error")
    assert sig.failure_in("[INFO] BUILD FAILURE").startswith("build failure")
    assert sig.failure_in("bash: foo: command not found")
    assert sig.failure_in("src/a.c:3: error: expected ';'")
    assert sig.failure_in("12 passed in 3s") is None
    assert sig.failure_in("test_error_handling PASSED") is None
    assert sig.waiting_for_input("Password: ")
    assert sig.waiting_for_input("Proceed? ")
    assert sig.waiting_for_input("Proceed?\n") is None
    assert sig.waiting_for_input("Compiling 3/10") is None
