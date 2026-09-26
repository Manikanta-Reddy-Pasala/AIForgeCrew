"""Running one pipeline (Doer) shell command to completion.

No fixed time limit: the command runs until it exits. It is stopped only when
it looks HUNG — no output and no CPU for ``AIFORGE_CMD_IDLE_S`` (default 600s;
see :mod:`aiforge_core.runtime.cmd_idle`) — or at an explicit wall clock
(``AIFORGE_SHELL_TIMEOUT``, when the operator sets it). A build that keeps
printing runs unbounded.

Output goes to temp files, not pipes: a pipe fills at ~64 KB and a verbose
build then blocks until killed, and a bare ``cmd &`` child holding the pipe
kept the call waiting for the full timeout. The command runs in its own
process group; when it exits, a leftover child still writing into our output
is stopped with it (a child that redirected its own output keeps running).
"""
from __future__ import annotations

import os
import subprocess
import time

_READ_MAX = 8 * 1024 * 1024


def _read_all(fh) -> str:
    """The stream's text: whole when modest, else its head and its tail (the
    8 KB the model sees is the head; the digest wants the error lines, which
    sit at the end)."""
    fh.flush()
    size = fh.seek(0, os.SEEK_END)
    fh.seek(0)
    if size <= _READ_MAX:
        return fh.read().decode("utf-8", "replace")
    head = fh.read(64 * 1024).decode("utf-8", "replace")
    fh.seek(size - _READ_MAX // 2)
    tail = fh.read().decode("utf-8", "replace")
    return head + "\n…\n" + tail


def run_to_completion(argv, cwd: str, wall_s: float, idle_s: float, *,
                      checkin_s: float = 0.0, cmd: str = "") -> dict:
    """``{"out", "err", "code", "why"}`` — ``why`` is None when the command
    exited on its own, else "timeout" (wall clock) or "hung" (idle).

    With ``checkin_s`` > 0 a command still running at the check-in — or whose
    output already shows an error or a prompt — is not waited out: it is
    handed to the job table and ``{"job": <what it printed so far>}`` comes
    back, for the Doer to command_wait / command_output / command_kill."""
    from aiforge_core.runtime.chat_agent._shell_wait import _checkin_due
    from aiforge_core.runtime.chat_agent._spool import Spool
    from aiforge_core.runtime.cmd_idle import ProgressClock
    spool = Spool()
    try:
        proc = subprocess.Popen(
            argv, shell=not isinstance(argv, list), cwd=cwd,
            stdout=spool.out, stderr=spool.err,
            start_new_session=True)
    except Exception:
        spool.close()
        raise
    spool.pgid = proc.pid
    why = None
    handed = False
    try:
        deadline = time.monotonic() + wall_s if wall_s > 0 else None
        checkin_at = time.monotonic() + checkin_s if checkin_s > 0 else None
        seen = [0]
        clock = ProgressClock(spool.pgid, spool.size, idle_s)
        while proc.poll() is None:
            if deadline is not None and time.monotonic() > deadline:
                why = "timeout"
            elif clock.stalled():
                why = "hung"
            elif spool.too_big():
                why = "too_big"
            if why:
                spool.kill_group()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            back = _checkin_due(checkin_at, spool, seen)
            if back and proc.poll() is None:
                from aiforge_core.runtime import cmd_jobs
                job = cmd_jobs.adopt_spooled(
                    proc, spool, cmd or str(argv), str(cwd), explicit=False,
                    idle_s=idle_s, deadline=deadline)
                handed = True
                return {"job": cmd_jobs.look(job, back)}
            time.sleep(0.1)
        return {"out": _read_all(spool.out), "err": _read_all(spool.err),
                "code": proc.returncode if why is None else None, "why": why}
    finally:
        if not handed:
            spool.release_children()
            spool.close()


__all__ = ["run_to_completion"]
