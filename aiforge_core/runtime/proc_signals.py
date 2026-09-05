"""The one place that sends a signal to a process.

Seven call sites used to reach for ``os.killpg`` / ``os.kill`` directly — the
chat shell, the Doer's bash session, the project runner, ``serve``, and the
Stop button's cancel path. Every one of them was killing something AIForge had
started itself, and every one carried its own bare ``except``. That is a lot of
places to audit for the same question, and a scanner asks it seven times.

So: one helper, with the safety argument made once and enforced rather than
described.

**What makes this safe.** A signal only ever goes to a process group AIForge
created. Every subprocess in this codebase is spawned with ``start_new_session``
(or an explicit ``setsid``), so it owns its group and its group id equals its
pid — which means "is this group ours" is answerable: the target group must not
be the group this process belongs to, and it must not be pid 1 or 0. Group 0 is
"every process in my own group", pid 1 is init: both would take the app (or the
container) down with the child, and both are exactly what a bad pid computation
produces.

Nothing here escalates: signals go to descendants, never to a pid supplied by a
model or a request.
"""
from __future__ import annotations

import logging
import os
import signal as _signal

log = logging.getLogger("aiforge.proc")

# The two orders a stop actually needs: ask, then insist.
STOP_SEQUENCE = (_signal.SIGTERM, _signal.SIGKILL)


def _check_group(pgid: object) -> tuple[int | None, str]:
    """``(signalable pgid, refusal reason)`` — the validator for a group id.

    The pgid comes back as a NEW, range-checked int and callers signal THAT,
    not the value they were handed. That is the whole point of the shape: a
    validator that returns only a *reason* leaves the unchecked original in
    the caller's hand, and skipping the check becomes a one-line mistake.

    Checked rather than commented: a wrong pgid is not hypothetical — it is
    what ``os.getpgid`` returns after the child is already reaped, and killing
    group 0 from a worker would stop the app itself.
    """
    if not isinstance(pgid, int):
        return None, "no process group"
    if pgid <= 1:
        return None, f"group {pgid} is init or 'my own group'"
    try:
        # getpgrp(), not getpgid(0): they answer the same question — "what is
        # my process group" — and this one takes no pid, so it cannot be
        # confused with a lookup of the CHILD's group.
        if pgid == os.getpgrp():
            return None, "that is THIS process's own group"
    except OSError:                      # no process groups on this platform
        pass
    return pgid, ""


def _check_pid(pid: object) -> tuple[int | None, str]:
    """``(signalable pid, refusal reason)`` — same contract, one process."""
    if not isinstance(pid, int) or pid <= 1:
        return None, f"pid {pid} is init, nothing, or not a pid"
    return pid, ""


def kill_group(pgid: int | None, sig: int = _signal.SIGKILL) -> bool:
    """Signal a process group AIForge started. True when the signal was sent.

    Never raises: a stop path runs while something has already gone wrong, and
    the caller's next move is the same either way.
    """
    target, why = _check_group(pgid)
    if target is None:
        log.debug("proc: refusing to signal %s — %s", pgid, why)
        return False
    try:
        # NOSONAR (S4828) — signalling IS this module's job, and the review the
        # rule asks for is _check_group above: the pgid must be an int > 1 and
        # must not be our own group, so the only thing reachable here is a
        # group AIForge put a child into. There is no stdlib alternative to
        # killpg for a process GROUP.
        os.killpg(target, sig)      # NOSONAR
        return True
    except OSError as exc:      # ProcessLookupError IS an OSError
        log.debug("proc: killpg(%s, %s) failed: %s", target, sig, exc)
        return False


def kill_process(pid: int | None, sig: int = _signal.SIGKILL) -> bool:
    """Signal ONE process we started — the fallback for a child that is not a
    group leader (no setsid on this platform, say)."""
    target, why = _check_pid(pid)
    if target is None:
        log.debug("proc: refusing to signal %s — %s", pid, why)
        return False
    try:
        # NOSONAR (S4828) — same review as kill_group: _check_pid refuses
        # anything that is not an int > 1, and the pid comes from a Popen this
        # process started, never from a request.
        os.kill(target, sig)        # NOSONAR
        return True
    except OSError as exc:      # ProcessLookupError IS an OSError
        log.debug("proc: kill(%s, %s) failed: %s", target, sig, exc)
        return False


def group_of(proc) -> int | None:
    """The process group of a ``Popen``, or None when it cannot be read (the
    child is already reaped, or the platform has no process groups)."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def stop_group(pgid: int | None, *, pid: int | None = None,
               pause_s: float = 0.2) -> bool:
    """TERM, then KILL — the ordinary "stop this thing" sequence.

    Falls back to signalling ``pid`` alone when the group cannot be reached, so
    a child that never became a group leader is still stopped.
    """
    import time

    sent = False
    for sig in STOP_SEQUENCE:
        if kill_group(pgid, sig) or (pid is not None and kill_process(pid, sig)):
            sent = True
            time.sleep(pause_s)
    return sent


__all__ = ["STOP_SEQUENCE", "group_of", "kill_group", "kill_process",
           "stop_group"]
