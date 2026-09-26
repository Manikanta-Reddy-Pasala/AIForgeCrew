"""Shell commands have no wall clock: a command is stopped when it goes silent
(no output, no CPU) for AIFORGE_CMD_IDLE_S, or at a timeout someone asked for.
A build that keeps printing runs past the idle window.

Real subprocesses throughout — the detector reads real output files and a real
process group, which a fake Popen would never exercise."""
from __future__ import annotations

import os
import time

import pytest

from aiforge_core.runtime import cmd_idle
from aiforge_core.runtime.chat_agent import _shell as S
from aiforge_core.runtime.doer_tools import _fs


@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    for k in ("AIFORGE_CHAT_CMD_TIMEOUT_S", "AIFORGE_SHELL_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(S, "_workspace_root", lambda: None)
    return tmp_path


# ── the clock ────────────────────────────────────────────────────────────

class _Ticks:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_output_growth_is_progress(monkeypatch):
    monkeypatch.setattr(cmd_idle, "group_cpu_s", lambda pgid: None)
    size, ticks = [0], _Ticks()
    c = cmd_idle.ProgressClock(1, lambda: size[0], 10, clock=ticks)
    ticks.t = 9
    assert not c.stalled()
    size[0] = 5
    ticks.t = 15
    assert not c.stalled()           # new output at 15 restarts the window
    ticks.t = 24
    assert not c.stalled()
    ticks.t = 26
    assert c.stalled()


def test_cpu_without_output_is_progress(monkeypatch):
    cpu = [0.0]
    monkeypatch.setattr(cmd_idle, "group_cpu_s", lambda pgid: cpu[0])
    ticks = _Ticks()
    c = cmd_idle.ProgressClock(1, lambda: 0, 100, clock=ticks)
    cpu[0] = 30.0                    # a quiet compile burning CPU
    ticks.t = 101
    assert not c.stalled()
    ticks.t = 202                    # ...then nothing at all
    assert c.stalled()


def test_zero_means_never(monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "0")
    assert cmd_idle.idle_limit_s() == 0.0
    c = cmd_idle.ProgressClock(None, lambda: 0, 0)
    assert not c.stalled()


def test_junk_idle_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "soon")
    assert cmd_idle.idle_limit_s() == 600.0


def test_wall_cap_only_when_asked(monkeypatch):
    monkeypatch.delenv("AIFORGE_X_TIMEOUT", raising=False)
    assert cmd_idle.wall_cap_s(None, "AIFORGE_X_TIMEOUT") == 0.0
    assert cmd_idle.wall_cap_s(30, "AIFORGE_X_TIMEOUT") == 30.0
    monkeypatch.setenv("AIFORGE_X_TIMEOUT", "45")
    assert cmd_idle.wall_cap_s(None, "AIFORGE_X_TIMEOUT") == 45.0
    monkeypatch.setenv("AIFORGE_X_TIMEOUT", "junk")
    assert cmd_idle.wall_cap_s(None, "AIFORGE_X_TIMEOUT") == 0.0


def test_group_cpu_sees_a_busy_child():
    import subprocess
    p = subprocess.Popen(["python3", "-c", "import time\nt=time.time()\n"
                          "while time.time()-t<1.5: pass"],
                         start_new_session=True)
    try:
        time.sleep(1.0)
        cpu = cmd_idle.group_cpu_s(p.pid)
        if cpu is None:
            pytest.skip("no psutil and no /proc here")
        assert cpu > 0.3
    finally:
        p.kill()
        p.wait()


# ── chat run_command, real processes ─────────────────────────────────────

def test_a_silent_hung_command_is_stopped_after_the_idle_window(chat_env,
                                                                monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "1")
    t0 = time.monotonic()
    res = S._t_run_command({"cmd": "echo started; sleep 30"}, str(chat_env))
    assert time.monotonic() - t0 < 10
    assert res["hung"] is True and res["timed_out"] is True
    assert "started" in res["stdout"]
    assert "HUNG" in res["error"]


def test_a_command_that_keeps_printing_outlives_the_idle_window(chat_env,
                                                                monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "1")
    res = S._t_run_command(
        {"cmd": "for i in 1 2 3 4 5 6; do echo tick $i; sleep 0.5; done"},
        str(chat_env))
    assert res["ok"] is True, res
    assert "tick 6" in res["stdout"]


def test_a_quiet_command_burning_cpu_is_not_hung(chat_env, monkeypatch):
    if cmd_idle.group_cpu_s(os.getpgid(0)) is None:
        pytest.skip("no CPU signal on this platform")
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "1")
    res = S._t_run_command(
        {"cmd": "python3 -c \"import time\nt=time.time()\n"
                "while time.time()-t<3.5: pass\nprint('built')\""},
        str(chat_env))
    assert res["ok"] is True, res
    assert "built" in res["stdout"]


def test_an_explicit_timeout_is_still_a_wall_clock(chat_env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    res = S._t_run_command(
        {"cmd": "while true; do echo x; sleep 0.2; done", "timeout": 1},
        str(chat_env))
    assert res["timed_out"] is True
    assert "hung" not in res
    assert "timed out after 1s" in res["error"]


def test_the_operator_knob_still_caps(chat_env, monkeypatch):
    monkeypatch.setenv("AIFORGE_CHAT_CMD_TIMEOUT_S", "1")
    res = S._t_run_command(
        {"cmd": "while true; do echo x; sleep 0.2; done"}, str(chat_env))
    assert res["timed_out"] is True


def test_a_bare_background_child_still_dies_with_the_command(chat_env,
                                                             monkeypatch):
    """`cmd & ; echo` in the middle of a command: the child writes into our
    output, so it is stopped when the command returns — not left running,
    and the call does not wait on it."""
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "600")
    marker = chat_env / "alive.txt"
    t0 = time.monotonic()
    res = S._t_run_command(
        {"cmd": f"(while true; do echo bg; date > {marker}; sleep 0.2; done) & "
                "echo fg"}, str(chat_env))
    assert time.monotonic() - t0 < 10
    assert res["ok"] is True and "fg" in res["stdout"]
    time.sleep(0.6)
    before = marker.read_text() if marker.exists() else ""
    time.sleep(1.2)
    after = marker.read_text() if marker.exists() else ""
    assert before == after          # nothing is writing any more


# ── doer run_shell, real processes ───────────────────────────────────────

@pytest.fixture
def doer_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import request_context
    monkeypatch.setattr(request_context, "get_repo_root", lambda: str(tmp_path))
    monkeypatch.delenv("AIFORGE_SHELL_TIMEOUT", raising=False)
    from aiforge_core.runtime.tools import command_risk
    monkeypatch.setattr(command_risk, "assess", lambda cmd: {"level": "safe"})
    return tmp_path


def test_doer_hung_command_is_stopped_and_keeps_its_output(doer_repo,
                                                           monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "1")
    t0 = time.monotonic()
    out = _fs.run_shell("echo partial; echo 'Traceback (most recent call last):'"
                        " >&2; sleep 30")
    assert time.monotonic() - t0 < 10
    assert out["ok"] is False and out["error"] == "timeout"
    assert "partial" in out["stdout"]
    assert "hung" in out["reason"]


def test_doer_streaming_build_is_not_cut_off(doer_repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_IDLE_S", "1")
    out = _fs.run_shell("for i in 1 2 3 4 5; do echo step $i; sleep 0.5; done")
    assert out["ok"] is True and out["returncode"] == 0
    assert "step 5" in out["stdout"]


def test_doer_operator_wall_clock(doer_repo, monkeypatch):
    monkeypatch.setenv("AIFORGE_SHELL_TIMEOUT", "1")
    out = _fs.run_shell("while true; do echo x; sleep 0.2; done")
    assert out["error"] == "timeout"
    assert "reason" not in out
