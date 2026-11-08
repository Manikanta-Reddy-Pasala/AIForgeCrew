"""Checking on a job that is still running is waiting: neither progress nor
a loop.

A long build, suite or deploy is started in the background and checked on
with ``command_wait`` / ``command_output``. A quiet job (a remote rollout, a
job blocked on the network) shows the same output and no CPU for minutes,
so each check looked like the last one: the no-progress rule nudged after 6
checks and paused after 6 more, and the stall guard nudged after 4 repeats
and paused after about 10. An unattended scheduled job that pauses is
reported as stopped. While the job's last check said it is still running,
a check on it is not counted by either, up to ``AIFORGE_CHAT_JOB_WAIT_MAX_S``
(two hours) from the first check that found it running; after that a job
that never ends is treated as stuck again.
"""
from __future__ import annotations

import os
import re
import time

CHECK_INS = frozenset({"command_wait", "command_output"})


def cap_s() -> float:
    try:
        return max(0.0, float(os.environ.get("AIFORGE_CHAT_JOB_WAIT_MAX_S", "7200")))
    except ValueError:
        return 7200.0


def _key(args, result=None) -> str:
    a = args if isinstance(args, dict) else {}
    r = result if isinstance(result, dict) else {}
    return str(a.get("id") or a.get("job") or a.get("ref") or r.get("id") or "")


def note_result(st, name, args, result) -> bool:
    """Record a check-in's answer. True when the job is still running and the
    wait is within the cap (the step is waiting, not idle)."""
    if name not in CHECK_INS or not isinstance(result, dict):
        return False
    key = _key(args, result)
    if not key:
        return False
    jobs = st.__dict__.setdefault("job_waits", {}) if hasattr(st, "__dict__") else {}
    if result.get("running") is True:
        first = jobs.setdefault(key, time.monotonic())
        return time.monotonic() - first <= cap_s()
    jobs.pop(key, None)                    # finished, killed or gone
    return False


#: A shell command that only waits and then looks: ``sleep 15; tail log``.
_SHELL_POLL = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?sleep\s+(\d+(?:\.\d+)?)\s*(?:;|&&)\s*(.*)$", re.S)
#: What may follow the sleep: commands that only look (a poll does no work).
_LOOKERS = frozenset({"tail", "cat", "head", "ls", "grep", "wc", "ps", "curl", "echo",
                      "test", "stat", "pgrep", "jobs", "date", "du", "df", "kubectl",
                      "docker", "systemctl", "journalctl"})
#: Shorter sleeps are not a wait.
_MIN_SLEEP_S = 5
_SHELL_TOOLS = frozenset({"run_command", "bash", "shell", "run_shell"})


def shell_poll(st, name, args) -> bool:
    """A ``sleep N; <look>`` command polling a job the shell way, within the
    cap from the first such poll (quietjob chat check, 11-08: six loop notes
    on a correct wait polled this way)."""
    if name not in _SHELL_TOOLS:
        return False
    a = args if isinstance(args, dict) else {}
    m = _SHELL_POLL.search(str(a.get("cmd") or a.get("command") or ""))
    if not m or float(m.group(1)) < _MIN_SLEEP_S:
        return False
    # every command after the sleep only looks (tail, cat, ls, kubectl get ...)
    for part in re.split(r"\s*(?:;|&&|\|\||\|)\s*", m.group(2).strip()):
        word = (part.split() or [""])[0]
        if word and word.rsplit("/", 1)[-1] not in _LOOKERS:
            return False
    first = st.__dict__.setdefault("shell_poll_since", time.monotonic()) \
        if hasattr(st, "__dict__") else time.monotonic()
    return time.monotonic() - first <= cap_s()


def waiting(st, name, args) -> bool:
    """Before the call runs: the last check on this job found it running, and
    the wait is within the cap; or the call is a shell ``sleep N; ...`` poll."""
    if shell_poll(st, name, args):
        return True
    if name not in CHECK_INS:
        return False
    first = (getattr(st, "job_waits", None) or {}).get(_key(args))
    return first is not None and time.monotonic() - first <= cap_s()
