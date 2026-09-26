"""The tmux ``bash`` tool is checked on, like run_shell — not waited out.

It used to block until its own 90 s timeout. Now a command still running at
the check-in (AIFORGE_CMD_CHECKIN_S), or printing an error or a prompt first,
comes back as a job that command_wait / command_output / command_kill work on;
the pane stays the session and is busy until the job ends.

The first half drives a FAKE tmux (runs everywhere); the second half drives the
real binary and skips cleanly where there is none.
"""
from __future__ import annotations

import shutil
import time
import types as pytypes

import pytest

from aiforge_core.runtime import cmd_jobs
from aiforge_core.runtime.chat_agent import _cmd_tools
from aiforge_core.runtime.tools import _tmux_job as TJ
from aiforge_core.runtime.tools import bash as B


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_leftover_jobs():
    yield
    for job in list(cmd_jobs._JOBS.values()):
        if isinstance(job, TJ.PaneJob):
            cmd_jobs._forget(job)
    TJ._BUSY.clear()


def _prompt(rc=0):
    return f"__AIFORGE_PROMPT_{B._NONCE}_{rc}__"


# ─── fake tmux ─────────────────────────────────────────────────────────

@pytest.fixture
def tmux(monkeypatch):
    state: dict = {"calls": [], "exists": False, "panes": [""]}
    monkeypatch.setattr(B, "_tmux_available", lambda: True)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)

    def _run(args, **kw):
        state["calls"].append(list(args))
        if args[:2] == ["tmux", "has-session"]:
            return pytypes.SimpleNamespace(
                returncode=0 if state["exists"] else 1, stdout=b"", stderr=b"")
        if args[:2] == ["tmux", "capture-pane"]:
            pane = state["panes"][0] if len(state["panes"]) == 1 \
                else state["panes"].pop(0)
            return pytypes.SimpleNamespace(returncode=0, stdout=pane.encode(),
                                           stderr=b"")
        return pytypes.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(B.subprocess, "run", _run)
    B._active_sessions.clear()
    yield state
    B._active_sessions.clear()


def test_a_failure_in_the_output_hands_the_command_back(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt(0)}\n",
                     f"{_prompt(0)}\npytest\nTraceback (most recent call last)\n"
                     "  File x\n"]
    res = B.bash("pytest", _run_id="r1")
    assert res["running"] is True
    assert res["id"].startswith("tmux-")
    assert "traceback" in res["returned_because"]
    assert "Traceback" in res["new_output"]
    assert "pytest" not in res["new_output"], "the echo is stripped"
    assert "command_wait" in res["hint"]


def test_a_prompt_waiting_for_input_hands_the_command_back(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt(0)}\n",
                     f"{_prompt(0)}\n./setup\nOverwrite config? [y/N] "]
    res = B.bash("./setup", _run_id="r1")
    assert res["running"] is True
    assert res["returned_because"].startswith("waiting for input")


def test_the_check_in_hands_back_a_quiet_long_command(tmux, repo_root,
                                                      monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    clock = {"t": 0.0}

    def _tick():
        clock["t"] += 0.3
        return clock["t"]
    monkeypatch.setattr(TJ.time, "monotonic", _tick)
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt(0)}\n",
                     f"{_prompt(0)}\nmvn package\n[INFO] compiling\n"]
    res = B.bash("mvn package", _run_id="r1")
    assert res["returned_because"] == "check-in: still running"
    assert "[INFO] compiling" in res["new_output"]


def test_a_busy_pane_is_never_typed_into(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt(0)}\n",
                     f"{_prompt(0)}\nbuild\nERROR: boom\n"]
    job_id = B.bash("build", _run_id="r1")["id"]
    typed = len([c for c in tmux["calls"] if c[:2] == ["tmux", "send-keys"]])
    res = B.bash("ls", _run_id="r1")
    assert res["ok"] is False and res["busy"] == job_id
    assert len([c for c in tmux["calls"]
                if c[:2] == ["tmux", "send-keys"]]) == typed


def test_a_quick_command_still_answers_in_one_call(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt(0)}\n",
                     f"{_prompt(0)}\necho hi\nhi\n{_prompt(0)}\n"]
    assert B.bash("echo hi", _run_id="r1") == {
        "ok": True, "returncode": 0, "command": "echo hi", "stdout": "hi",
        "truncated": False}
    assert TJ.busy("aiforge-r1") is None


def test_a_redrawn_progress_line_is_not_appended_twice(tmp_path, monkeypatch):
    run = TJ.PaneRun("p", "dl", 1)
    screens = iter([f"{_prompt(0)}\ndl\nstart\n 10%",
                    f"{_prompt(0)}\ndl\nstart\n 55%",
                    f"{_prompt(0)}\ndl\nstart\n100%\nok\n{_prompt(0)}\n"])
    monkeypatch.setattr(TJ, "_capture", lambda name: next(screens))
    run.refresh()
    assert run.tail == " 10%"
    run.refresh()
    run.refresh()
    with open(run.path, encoding="utf-8") as fh:
        assert fh.read() == "start\n100%\nok\n"
    assert run.done and run.returncode == 0
    run.close()


def test_history_trimmed_under_a_long_command_still_finds_its_end(monkeypatch):
    run = TJ.PaneRun("p", "big", 2)
    screens = iter([f"{_prompt(0)}\nold\n{_prompt(0)}\nbig\nl1\n",
                    "l2\nl3\n",                 # both prompts scrolled away
                    f"l3\nl4\n{_prompt(3)}\n"])
    monkeypatch.setattr(TJ, "_capture", lambda name: next(screens))
    assert run.poll() is None
    assert run.poll() is None
    assert run.poll() == 3
    run.close()


# ─── real tmux ─────────────────────────────────────────────────────────

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None,
                                 reason="tmux binary not on PATH")


@pytest.fixture
def real(repo_root, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_CHECKIN_S", "1")
    run_id = f"checkin-{time.monotonic_ns()}"
    yield run_id
    B.destroy_session(run_id)


@_needs_tmux
def test_real_consecutive_commands_read_their_own_output(real):
    """The old drain took the LAST TWO prompts on screen — the previous
    command's — and answered a slow command instantly with stale output."""
    assert B.bash("echo one", _run_id=real)["stdout"] == "one"
    assert B.bash("echo two", _run_id=real)["stdout"] == "two"
    res = B.bash("sleep 0.5; echo three", _run_id=real)
    assert res["stdout"] == "three"


@_needs_tmux
def test_real_long_command_becomes_a_job_and_keeps_the_session(real,
                                                               repo_root):
    (repo_root / "sub").mkdir()
    t0 = time.monotonic()
    res = B.bash("cd sub && sleep 3 && echo built", _run_id=real)
    assert time.monotonic() - t0 < 2.5, "it waited instead of checking in"
    assert res["running"] is True
    assert B.bash("echo hi", _run_id=real)["busy"] == res["id"]
    done = _cmd_tools._t_command_wait({"id": res["id"], "max_s": 20}, "")
    assert done["running"] is False and done["code"] == 0
    assert "built" in done["new_output"]
    assert B.bash("pwd", _run_id=real)["stdout"].endswith("/sub"), \
        "the pane is still the session"


@_needs_tmux
def test_real_failure_returns_early_and_kill_frees_the_pane(real):
    t0 = time.monotonic()
    res = B.bash("echo 'npm ERR! missing script'; sleep 60", _run_id=real)
    assert time.monotonic() - t0 < 2.5
    assert "npm error" in res["returned_because"]
    killed = _cmd_tools._t_command_kill({"id": res["id"]}, "")
    assert killed["killed"] is True and killed["running"] is False
    assert B.bash("echo after", _run_id=real)["stdout"] == "after"


@_needs_tmux
def test_real_stuck_command_is_reported_by_command_wait(real, monkeypatch):
    monkeypatch.setenv("AIFORGE_CMD_STUCK_CHECK_S", "1")
    res = B.bash("sleep 60", _run_id=real)
    assert res["running"] is True
    t0 = time.monotonic()
    out = _cmd_tools._t_command_wait({"id": res["id"], "max_s": 30}, "")
    assert out.get("stuck") is True, out
    assert time.monotonic() - t0 < 10
    _cmd_tools._t_command_kill({"id": res["id"]}, "")


@_needs_tmux
def test_real_prompt_is_seen_without_a_newline(real):
    res = B.bash("read -p 'Continue? [y/N] ' x; echo got $x", _run_id=real)
    assert res["returned_because"].startswith("waiting for input")
    _cmd_tools._t_command_kill({"id": res["id"]}, "")
