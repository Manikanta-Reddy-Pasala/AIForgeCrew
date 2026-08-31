"""The persistent shell, driven against a FAKE tmux.

The real tmux tests in test_bash.py skip on a box without tmux — which was
every box for a while, and that is exactly how three real defects hid. So this
file stubs tmux at the subprocess seam and runs everywhere.

What the stubs let us pin down is the awkward part of a pty-backed shell: the
pane is a screen, not a stream. Output is read back by finding two prompt
sentinels and taking what lies between them, the sentinel carries a per-boot
random nonce so a command that PRINTS something sentinel-shaped cannot forge
one, and the shell echoes the command back into the pane before its output —
so the echo (possibly wrapped across pane-width lines) has to be stripped, or
the agent reads back its own command as the answer.

The tmux-less fallback has its own hazard: a daemon grandchild inheriting the
stdout pipe keeps communicate() blocked forever after the main process exits,
so every drain is bounded and ends in a process-group kill.
"""
from __future__ import annotations

import subprocess
import types as pytypes

import pytest

from aiforge_core.runtime.tools import bash as B


@pytest.fixture()
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    return tmp_path


def _prompt(rc=0):
    return f"__AIFORGE_PROMPT_{B._NONCE}_{rc}__"


@pytest.fixture()
def tmux(monkeypatch):
    """A fake tmux: records every command, replays a scripted pane."""
    state: dict = {"calls": [], "exists": False, "panes": [""], "rc": 0}
    monkeypatch.setattr(B, "_tmux_available", lambda: True)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)

    def _run(args, **kw):
        state["calls"].append(list(args))
        if args[:2] == ["tmux", "has-session"]:
            return pytypes.SimpleNamespace(returncode=0 if state["exists"] else 1,
                                           stdout=b"", stderr=b"")
        if args[:2] == ["tmux", "capture-pane"]:
            pane = state["panes"][0] if len(state["panes"]) == 1 \
                else state["panes"].pop(0)
            return pytypes.SimpleNamespace(returncode=0,
                                           stdout=pane.encode(), stderr=b"")
        return pytypes.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(B.subprocess, "run", _run)
    B._active_sessions.clear()
    yield state
    B._active_sessions.clear()


def _sent(state):
    """The keystrokes typed into the pane, in order."""
    return [c[4] for c in state["calls"] if c[:2] == ["tmux", "send-keys"]]


# ─── which session a call belongs to ───────────────────────────────────


def test_a_call_with_no_run_lands_in_one_stable_session():
    """A per-call uuid would leak a tmux session on every bash call."""
    B.set_run_id(None)
    assert B._effective_run_id(None) == "default"


def test_the_runner_binds_the_session_for_the_whole_run():
    B.set_run_id("adk-42")
    try:
        assert B._effective_run_id(None) == "adk-42"
        assert B._effective_run_id("explicit") == "explicit"
    finally:
        B.set_run_id(None)


# ─── reading a pane back ───────────────────────────────────────────────


def test_the_output_is_what_lies_between_two_prompts(tmux):
    tmux["panes"] = [f"{_prompt()}\nls\nfile.txt\n{_prompt(0)}\n"]
    body, rc, timed_out = B._drain_until_prompt("s", timeout=1)
    assert body == "ls\nfile.txt" and rc == 0 and timed_out is False


def test_the_exit_code_comes_off_the_closing_prompt(tmux):
    tmux["panes"] = [f"{_prompt()}\nboom\n{_prompt(2)}\n"]
    assert B._drain_until_prompt("s", timeout=1)[1] == 2


def test_a_command_printing_a_prompt_shaped_string_cannot_forge_one(tmux):
    """The nonce is per-boot and unpredictable, so program output can't be
    mis-parsed as a shell prompt (wrong rc / truncated stdout)."""
    tmux["panes"] = [f"{_prompt()}\necho __AIFORGE_PROMPT_9__\n{_prompt(0)}\n"]
    body, rc, _ = B._drain_until_prompt("s", timeout=1)
    assert rc == 0 and "__AIFORGE_PROMPT_9__" in body


def test_a_pane_that_never_settles_times_out(tmux, monkeypatch):
    tmux["panes"] = ["still running…"]
    ticks = iter([0.0, 0.5, 9.0, 9.0])
    monkeypatch.setattr(B.time, "monotonic", lambda: next(ticks))
    body, rc, timed_out = B._drain_until_prompt("s", timeout=1)
    assert timed_out is True and rc is None and body == "still running…"


def test_the_first_prompt_after_create_needs_only_one_sentinel(tmux):
    tmux["panes"] = [f"{_prompt(0)}\n"]
    assert B._drain_until_prompt("s", 5, expect_initial_only=True) == ("", 0,
                                                                      False)


def test_stop_interrupts_the_running_command(tmux, monkeypatch):
    """The tmux path used to ignore cancellation, so Stop did nothing until
    the timeout expired."""
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: 7)
    monkeypatch.setattr(chat_cancel, "is_cancelled", lambda sid: True)
    tmux["panes"] = ["nothing yet"]
    body, rc, timed_out = B._drain_until_prompt("s", timeout=30)
    assert rc == -130 and timed_out is False
    assert ["tmux", "send-keys", "-t", "s", "C-c"] in tmux["calls"]


def test_no_chat_session_means_nothing_to_cancel_against(tmux, monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    tmux["panes"] = [f"{_prompt()}\nout\n{_prompt(0)}\n"]
    assert B._drain_until_prompt("s", timeout=1)[1] == 0


# ─── creating and destroying the session ───────────────────────────────


def test_the_session_is_created_with_a_parseable_prompt(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n"]
    assert B._create_session("r1") == "aiforge-r1"
    typed = _sent(tmux)
    assert typed[0] == B._PROMPT_PS1 and typed[1] == "clear"
    assert B._active_sessions["r1"] == "aiforge-r1"


def test_an_existing_session_is_reused(tmux):
    tmux["exists"] = True
    assert B._create_session("r1") == "aiforge-r1"
    assert _sent(tmux) == [], "no re-init on a live session"


def test_destroying_a_session_kills_it(tmux):
    tmux["exists"] = True
    B._active_sessions["r1"] = "aiforge-r1"
    B.destroy_session("r1")
    assert ["tmux", "kill-session", "-t", "aiforge-r1"] in tmux["calls"]
    assert "r1" not in B._active_sessions


def test_destroying_a_session_that_is_gone_is_quiet(tmux):
    B.destroy_session("r1")
    assert not any(c[:2] == ["tmux", "kill-session"] for c in tmux["calls"])


def test_nothing_is_killed_without_tmux(monkeypatch):
    monkeypatch.setattr(B, "_tmux_available", lambda: False)
    monkeypatch.setattr(B.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran tmux"))
    B.destroy_session("r1")


# ─── the tmux path end to end ──────────────────────────────────────────


def test_a_command_runs_in_the_persistent_session(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n",
                     f"{_prompt()}\necho hi\nhi\n{_prompt(0)}\n"]
    res = B.bash("echo hi", _run_id="r1")
    assert res["ok"] is True and res["returncode"] == 0
    assert res["stdout"] == "hi", "the pty echo of the command is stripped"


def test_a_failing_command_is_not_ok(tmux, repo_root):
    tmux["panes"] = [f"{_prompt(0)}\n",
                     f"{_prompt()}\nfalse\n{_prompt(1)}\n"]
    res = B.bash("false", _run_id="r1")
    assert res["ok"] is False and res["returncode"] == 1


def test_a_trailing_ampersand_returns_at_once(tmux, repo_root):
    """A long-running server must not hold the agent's turn."""
    tmux["panes"] = [f"{_prompt(0)}\n"]
    res = B.bash("npm run dev &", _run_id="r1")
    assert res == {"ok": True, "command": "npm run dev &", "backgrounded": True,
                   "returncode": 0, "stdout": "", "truncated": False}


def test_a_hung_command_is_interrupted_and_its_partial_output_kept(tmux,
                                                                   repo_root,
                                                                   monkeypatch):
    clock = {"t": 0.0}

    def _tick():
        clock["t"] += 0.4          # 1s budget → a couple of polls, then out
        return clock["t"]
    monkeypatch.setattr(B.time, "monotonic", _tick)
    tmux["panes"] = [f"{_prompt(0)}\n", "half an answer"]
    res = B.bash("sleep 999", timeout=1, _run_id="r1")
    assert res["ok"] is False and res["error"] == "timeout"
    assert res["truncated"] is True and "half an answer" in res["stdout"]
    assert ["tmux", "send-keys", "-t", "aiforge-r1", "C-c"] in tmux["calls"]


def test_a_restart_wipes_the_session_first(tmux, repo_root, monkeypatch):
    tmux["exists"] = True
    killed: list = []
    monkeypatch.setattr(B, "destroy_session", lambda rid: killed.append(rid))
    tmux["panes"] = [f"{_prompt()}\nx\n{_prompt(0)}\n"]
    B.bash("echo x", restart=True, _run_id="r1")
    assert killed == ["r1"]


def test_a_huge_answer_is_capped(tmux, repo_root):
    big = "y" * (B._STDOUT_CAP_BYTES + 500)
    tmux["panes"] = [f"{_prompt(0)}\n", f"{_prompt()}\nc\n{big}\n{_prompt(0)}\n"]
    res = B.bash("c", _run_id="r1")
    assert len(res["stdout"]) == B._STDOUT_CAP_BYTES and res["truncated"] is True


def test_an_empty_command_never_reaches_a_shell(tmux):
    assert B.bash("   ")["error"] == "empty_command"
    assert tmux["calls"] == []


def test_a_delete_is_refused_before_it_runs(tmux, monkeypatch):
    from aiforge_core.runtime.tools import delete_guard
    monkeypatch.setattr(delete_guard, "allow_delete", lambda: False)
    res = B.bash("rm -rf build")
    assert res["blocked"] == "delete" and tmux["calls"] == []


def test_the_docker_sandbox_takes_precedence_when_opted_in(tmux, monkeypatch):
    from aiforge_core.runtime import docker_sandbox
    monkeypatch.setattr(docker_sandbox, "is_enabled", lambda: True)
    monkeypatch.setattr(docker_sandbox, "exec_in_container",
                        lambda rid, cmd, timeout=None: {"ok": True,
                                                        "in": rid})
    assert B.bash("ls", _run_id="r1") == {"ok": True, "in": "r1"}
    assert tmux["calls"] == []


# ─── stripping the pty echo ────────────────────────────────────────────


def test_an_echo_split_across_pane_width_is_consumed():
    body = "echo a very long comm\nand here\nthe real output"
    assert B._strip_echoed_command(body, "echo a very long command here") \
        == "the real output"


def test_a_body_that_never_accounts_for_the_command_is_kept_whole():
    """Better to hand back an extra line than to guess bytes away."""
    body = "echo a very long comm"
    assert B._strip_echoed_command(body, "echo a very long command here") == body


def test_nothing_is_stripped_from_an_empty_body():
    assert B._strip_echoed_command("", "ls") == ""
    assert B._strip_echoed_command("out", "   ") == "out"


# ─── the tmux-less fallback ────────────────────────────────────────────


class _Proc:
    """A subprocess that exits after ``polls`` polls."""

    def __init__(self, polls=1, rc=0, out=b"out", err=b"", hang=False):
        self.pid = 4242
        self._polls = polls
        self.returncode = None
        self._rc = rc
        self._out, self._err = out, err
        self._hang = hang
        self.communicated = 0

    def poll(self):
        self._polls -= 1
        if self._polls <= 0:
            self.returncode = self._rc
            return self._rc
        return None

    def communicate(self, timeout=None):
        self.communicated += 1
        if self._hang and self.communicated == 1:
            raise subprocess.TimeoutExpired("cmd", timeout or 0)
        return self._out, self._err


@pytest.fixture()
def spawn(monkeypatch):
    """A stubbed Popen plus a no-op process-group kill."""
    state: dict = {"proc": _Proc(), "killed": []}
    monkeypatch.setattr(B.subprocess, "Popen",
                        lambda *a, **k: state.update(kw=k) or state["proc"])
    monkeypatch.setattr(B.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(B.os, "killpg",
                        lambda pgid, sig: state["killed"].append((pgid, sig)))
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    return state


@pytest.fixture()
def cancel(monkeypatch):
    from aiforge_core.runtime import chat_cancel
    state: dict = {"sid": 7, "cancelled": False, "pgids": []}
    monkeypatch.setattr(chat_cancel, "active", lambda: state["sid"])
    monkeypatch.setattr(chat_cancel, "is_cancelled",
                        lambda sid: state["cancelled"])
    monkeypatch.setattr(chat_cancel, "track_pgid",
                        lambda sid, pgid: state["pgids"].append((sid, pgid)))
    return state


def test_a_chat_run_gets_its_own_process_group(spawn, cancel, repo_root,
                                               monkeypatch):
    """So Stop can kill a whole build tree, not just the shell."""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    res = B._fallback_run("make", 30)
    assert res["ok"] is True and res["stdout"] == "out"
    assert spawn["kw"]["start_new_session"] is True
    assert cancel["pgids"] == [(7, 999)]


def test_stop_kills_the_tree_mid_build(spawn, cancel, repo_root):
    spawn["proc"] = _Proc(polls=5)
    cancel["cancelled"] = True
    res = B._run_cancellable("make", 30, 7,
                             __import__("aiforge_core.runtime.chat_cancel",
                                        fromlist=["x"]))
    assert res["stopped"] is True and res["error"] == "stopped by user"
    assert spawn["killed"] == [(999, 9)]


def test_a_run_already_stopped_never_spawns(spawn, cancel, repo_root,
                                            monkeypatch):
    cancel["cancelled"] = True
    monkeypatch.setattr(B.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("spawned after Stop"))
    assert B._fallback_run("make", 30)["stopped"] is True


def test_a_command_that_outlives_its_budget_is_killed(spawn, cancel, repo_root,
                                                      monkeypatch):
    import time as _t
    spawn["proc"] = _Proc(polls=99)
    ticks = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(_t, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    res = B._run_cancellable("sleep 999", 1, 7,
                             __import__("aiforge_core.runtime.chat_cancel",
                                        fromlist=["x"]))
    assert res["error"] == "timeout" and res["truncated"] is True
    assert spawn["killed"] == [(999, 9)]


def test_a_daemon_grandchild_cannot_block_the_drain_forever(spawn):
    """`npm run dev &` inherits the stdout pipe; communicate() would never
    return, so the drain is bounded and ends in a group kill."""
    proc = _Proc(hang=True)
    assert B._drain_bounded(proc) == (b"out", b"")
    assert spawn["killed"] == [(999, 9)]


def test_a_process_that_will_not_reap_still_returns(spawn):
    class _Stuck(_Proc):
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("cmd", 5)
    assert B._kill_group_and_reap(_Stuck()) == (b"", b"")


def test_a_spawn_failure_is_a_soft_error(spawn, cancel, repo_root, monkeypatch):
    monkeypatch.setattr(B.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no sh")))
    res = B._fallback_run("make", 30)
    assert res["ok"] is False and "no sh" in res["error"]


def test_outside_a_chat_run_the_plain_path_is_used(spawn, repo_root,
                                                   monkeypatch):
    from aiforge_core.runtime import chat_cancel
    monkeypatch.setattr(chat_cancel, "active", lambda: None)
    monkeypatch.setattr(B.subprocess, "run",
                        lambda *a, **k: pytypes.SimpleNamespace(
                            returncode=0, stdout=b"hi", stderr=b""))
    assert B._fallback_run("echo hi", 5)["stdout"] == "hi"
