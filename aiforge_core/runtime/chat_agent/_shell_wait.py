"""Waiting on a foreground ``run_command``: Stop / steer, the optional wall
clock, the output-idle detector, and collecting what it printed.

No fixed time limit by default. A command is ended early only when it looks
HUNG — no output and no CPU for ``AIFORGE_CMD_IDLE_S`` (see
:mod:`aiforge_core.runtime.cmd_idle`) — or when a wall clock was asked for
(the model's ``timeout`` argument, or the operator's
``AIFORGE_CHAT_CMD_TIMEOUT_S``). A build that keeps printing runs unbounded.
"""
from __future__ import annotations

import os
import subprocess


def _max_obs() -> int:
    from ._shell import _MAX_OBS
    return _MAX_OBS


def _drain(proc, timeout: float = 5) -> tuple[str, str] | None:
    """Whatever the process buffered, or None if it could not be collected."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return out or "", err or ""


def _hung_error(idle_s: float) -> str:
    return (f"stopped: no output and no CPU activity for {int(idle_s)}s — the "
            "command looks HUNG (waiting on input? a prompt? a network call "
            "that never returns?). PARTIAL output above. Next: make it "
            "non-interactive (--yes / -y / CI=1 / < /dev/null), or run a "
            "narrower command. Do NOT undo your edits over this.")


def _timeout_error(timeout: float) -> str:
    return (f"timed out after {int(timeout)}s — PARTIAL output "
            "above. This is not a failure of your change: the command "
            "just ran longer than the limit. Next: run a NARROWER "
            "command (one test file or a single test case), or re-issue "
            "this exact command with a larger \"timeout\" (e.g. 600). Do "
            "NOT undo your edits over a timeout.")


def _timeout_result(proc, timeout: float, spool=None, *,
                    hung: bool = False) -> dict:
    """Capture whatever the command buffered BEFORE we kill it, so the agent
    sees partial output (e.g. which tests ran/passed before the hang) and can
    adapt — instead of a blind "timeout" with no signal."""
    import signal as _sig

    from aiforge_core.runtime import proc_signals
    proc_signals.kill_group(proc_signals.group_of(proc), _sig.SIGTERM)
    if spool is not None:
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — ignored SIGTERM
            pass
        # The shell may be gone while a child that ignored SIGTERM is not.
        spool.kill_group()
        drained = spool.read()
    else:
        drained = _drain(proc)
    if drained is None:
        _kill_proc(proc)
        drained = ("", "")
    out, err = drained
    cap = _max_obs()
    res = {"ok": False, "timed_out": True, "code": None,
           "stdout": out[-cap:], "stderr": err[-cap:],
           "error": _hung_error(timeout) if hung else _timeout_error(timeout)}
    if hung:
        res["hung"] = True
    return res


def _progress_clock(spool, idle_s: float):
    if spool is None or not idle_s:
        return None
    from aiforge_core.runtime.cmd_idle import ProgressClock
    return ProgressClock(spool.pgid, spool.size, idle_s)


def _await_exit(proc, timeout: float, sid, spool=None,
                idle_s: float = 0.0) -> dict | None:
    """Poll until the process exits; a dict when it was stopped, went idle
    (hung) or ran past an explicit wall clock (``timeout`` > 0)."""
    import time as _time

    from aiforge_core.runtime.run_interrupt import (
        attention, steered, process_owned_by_watch)
    deadline = _time.monotonic() + timeout if timeout and timeout > 0 else None
    clock = _progress_clock(spool, idle_s)
    while proc.poll() is None:
        # Stop still kills immediately. A typed message is read first: the
        # command is the task, so it keeps running unless the message asks
        # to stop or replace it. A watch probe is stricter: only the
        # six-word stop set (and the Stop button) ends that wait.
        if process_owned_by_watch():
            why = attention(sid, only_cut=True)
        else:
            why = attention(sid, only_replace=True)
        if why == "stop":
            _kill_proc(proc)
            return {"ok": False, "stopped": True, "error": "stopped by user"}
        if why == "steer":
            _kill_proc(proc)
            return steered()
        if deadline is not None and _time.monotonic() > deadline:
            return _timeout_result(proc, timeout, spool)
        if clock is not None and clock.stalled():
            return _timeout_result(proc, idle_s, spool, hung=True)
        if spool is not None and spool.too_big():
            spool.kill_group()
            _kill_proc(proc)
            out, err = spool.read()
            cap = _max_obs()
            return {"ok": False, "code": None, "stdout": out[-cap:],
                    "stderr": err[-cap:], "error": spool.too_big_error()}
        _time.sleep(0.2)
    return None


def _collect_output(proc, spool=None) -> tuple[str, str]:
    """Bound communicate(): a daemon grandchild inheriting the stdout pipe
    (e.g. `npm run dev &`) keeps it open after the process exits, so an
    un-timed communicate() blocks forever even past the deadline. Spooled
    output is simply read back."""
    if spool is not None:
        return spool.read()
    try:
        ct = int(os.environ.get("AIFORGE_COMMUNICATE_TIMEOUT_S", "10"))
    except (TypeError, ValueError):
        ct = 10
    try:
        out, err = proc.communicate(timeout=ct)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_proc(proc)
        return _drain(proc) or ("", "")


def _kill_proc(proc) -> None:
    from aiforge_core.runtime import proc_signals
    if not proc_signals.stop_group(proc_signals.group_of(proc),
                                   pid=getattr(proc, "pid", None),
                                   pause_s=0.0):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — already gone
            pass
    # Reap so the killed child's pipe FDs are freed (no zombie leak).
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass
