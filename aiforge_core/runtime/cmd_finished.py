"""What a checked-on command proved once it FINISHED — as a normal run result.

A test suite that runs longer than the check-in (or prints ``FAILED`` early)
comes back to the model as a job, and its end arrives through
``command_wait`` / ``command_output`` / ``command_kill``. Those results carry
only the output since the last look, so the loop rules that read a
``run_command`` result — the same failure after another fix, fewer failing
tests, a full suite run green before the final — never saw the run end.

When a job finishes, :mod:`aiforge_core.runtime.cmd_jobs` records the tail of
its WHOLE output and its exit code here, keyed by the job id. :func:`as_run`
turns a check-in result that reports that end back into
``("run_command", {"cmd": <the original command>}, <result>)`` — the same
shape a run that finished inside the check-in window has — so varied check-in
calls on one command collapse to one failure signature, and a green run
counts under the same strict rules (unpiped, unnarrowed, the runner's own
pass line). A job still running is never a result: its partial output is
neither a pass nor a failure.
"""
from __future__ import annotations

import collections
import threading

#: Characters of each stream kept for the fingerprint (the tail: summaries
#: and failing-test lists sit at the end).
TAIL_CHARS = 64_000
_MAX = 64
_FINAL: collections.OrderedDict = collections.OrderedDict()
_LOCK = threading.Lock()
_CHECK_INS = frozenset({"command_wait", "command_output", "command_kill"})
_SHELL = frozenset({"run_command", "bash", "shell", "run", "run_shell"})


def record(key: str, cmd: str, code, stdout: str, stderr: str, *,
           stopped: str | None = None) -> dict:
    """Remember how job ``key`` ended. ``stopped`` names who ended it (the
    user, the model, a guard): then it is not the code's failure."""
    res = {"ok": code == 0 and not stopped, "code": code,
           "stdout": stdout or "", "stderr": stderr or "", "cmd": cmd}
    if stopped:
        res.update(stopped=True, error=stopped)
    with _LOCK:
        _FINAL.pop(key, None)
        _FINAL[key] = res
        while len(_FINAL) > _MAX:
            _FINAL.popitem(last=False)
    return res


def get(key) -> dict | None:
    with _LOCK:
        return _FINAL.get(str(key or ""))


def is_running(result) -> bool:
    """A result that reports a command still running (a hand-off, a
    check-in, a background start): neither a pass nor a failure."""
    return isinstance(result, dict) and (
        result.get("running") is True or bool(result.get("background")))


def as_run(name, args, result):
    """``(name, args, result)`` to judge as a finished run, or None.

    A result that finished inside its call is itself; a hand-off or a
    check-in on a job that has ended is the job's recorded end, attributed
    to the original command; anything still running, and a check-in with no
    recorded end, is None."""
    if not isinstance(result, dict) or is_running(result):
        return None
    if (result.get("running") is False and result.get("id")
            and (name in _CHECK_INS or name in _SHELL)):
        final = get(result.get("id"))
        if final is None:
            return None
        return "run_command", {"cmd": final.get("cmd") or ""}, final
    if name in _CHECK_INS:
        return None
    return name, args, result


def reset() -> None:
    with _LOCK:
        _FINAL.clear()


def trip_reason(key) -> str | None:
    """A last-resort guard of the background watcher ended job ``key``
    (idle, wall clock, output too large)."""
    key = str(key)
    if not key.startswith("bg-"):
        return None
    from aiforge_core.runtime import bg_commands
    return bg_commands.trip_reason(key[3:])


def _whole_tail(stream) -> str:
    from aiforge_core.runtime import cmd_signals as sig
    end = sig.file_size(stream)
    start = max(0, end - TAIL_CHARS)
    return sig.clean(sig.read_range(stream, start, end))


def record_job(job, code, ended_by) -> None:
    """A cmd_jobs job's WHOLE output tail and exit code, for the loop rules."""
    try:
        out = _whole_tail(job.streams[0])
        err = _whole_tail(job.streams[1]) if len(job.streams) > 1 else ""
        record(job.key, job.cmd, code, out, err, stopped=ended_by)
    except Exception:  # noqa: BLE001 — a record must never break a look
        pass


__all__ = ["TAIL_CHARS", "as_run", "get", "is_running", "record",
           "record_job", "reset", "trip_reason"]
