"""A turn answers while a command it handed off is still running.

``cmd_jobs.end_turn`` kills a turn's handed-off jobs when the turn ends, so
an answer that says "the build is still running" would be wrong a moment
later. When the model answers anyway, the still-running jobs are PROMOTED to
explicit background jobs instead: they outlive the turn, Stop still ends them
(a ``bg_work`` row), and their outcome is posted to the chat when they finish
— the same as a command started with ``background: true``. The answer gets a
line saying so (:func:`answer_suffix`).

A promoted pane (tmux) command is watched here: an input prompt ("[y/N]",
"Password:") is posted to the chat once, and a command with no output and no
CPU for ``AIFORGE_CMD_IDLE_S`` is killed as hung, so it cannot hold the pane
(and every later ``bash`` of the session) forever. When the watch ends the
job is forgotten. A job with no chat session is NOT promoted — nothing could
reach it to stop it — and dies with its turn as before.
"""
from __future__ import annotations

import logging
import threading
import time

from aiforge_core.runtime import cmd_jobs

log = logging.getLogger("aiforge.cmd_jobs")

_POLL_S = 1.0


def promote_turn_jobs() -> list:
    """Promote this turn's still-running handed-off jobs; returns them."""
    try:
        live = cmd_jobs.turn_running()
    except Exception:  # noqa: BLE001 — never block an answer on this
        return []
    done = []
    for job in live:
        try:
            if promote(job):
                done.append(job)
        except Exception:  # noqa: BLE001
            log.debug("promote %s failed", getattr(job, "key", "?"),
                      exc_info=True)
    return done


def promote(job) -> bool:
    """``job`` becomes an explicit background job (see the module doc).
    False when it is not promoted: already explicit, or sessionless (no
    chat to report to, no Stop to end it — it dies with its turn)."""
    if job.explicit or job.session_id is None:
        return False
    job.explicit = True
    opts = getattr(job, "watch_opts", None)
    if isinstance(opts, dict):
        # A spooled command already has a bg_work row and watcher: it only
        # stayed quiet because the agent was watching it. Now it reports.
        opts["announce"] = True
        return True
    _watch_in_background(job)
    return True


def _watch_in_background(job) -> None:
    """A job with no bg_work watcher (a tmux pane command): give it a row —
    so Stop ends it — and a thread that posts its outcome to the chat."""
    from aiforge_core.runtime import bg_work
    payload = {"cmd": job.cmd, **({"owner": job.owner} if job.owner else {})}
    # No pid/pgid on the row: a pane's foreground group changes command by
    # command; the job resolves the current one itself when it is killed.
    wid = bg_work._insert(job.session_id, "command", "", payload)
    ev = bg_work._bind(wid)
    threading.Thread(target=_watch, args=(job, wid, ev), daemon=True,
                     name=f"bg-promoted-{wid}").start()


def _idle_clock(job):
    """No output and no CPU for AIFORGE_CMD_IDLE_S: the hung-command clock
    (the pane's current foreground group for a tmux job)."""
    from aiforge_core.runtime.cmd_idle import ProgressClock, idle_limit_s
    idle = idle_limit_s()
    if hasattr(job.proc, "current_pgid"):
        from aiforge_core.runtime.tools._tmux_job import _PaneClock
        return _PaneClock(job.proc, job.size, idle)
    return ProgressClock(getattr(job, "pgid", None), job.size, idle)


def _prompt(job) -> str | None:
    from aiforge_core.runtime import cmd_signals as sig
    try:
        tail = job._tail()
    except Exception:  # noqa: BLE001
        return None
    if not sig.waiting_for_input(tail):
        return None
    return (tail.strip().splitlines() or [""])[-1][-160:]


def _watch(job, wid: int, ev: threading.Event) -> None:
    from aiforge_core.runtime import bg_work
    short = (job.cmd or "").strip().replace("\n", " ")[:80]
    clock = _idle_clock(job)
    asked = None
    try:
        while job.alive():
            if ev.is_set():
                job.kill("stopped: Stop")
                break
            prompt = _prompt(job)
            if prompt and prompt != asked:
                asked = prompt
                bg_work._post(job.session_id, f"Background job {job.key} is "
                              f"waiting for input: {prompt}")
            if clock.stalled():
                job.kill(f"stopped: no output and no CPU activity for "
                         f"{int(clock.idle_s)}s — the command looks HUNG"
                         + (" (waiting for input)" if asked else ""))
                break
            time.sleep(_POLL_S)
        code = getattr(job.proc, "returncode", None)
        stopped = ev.is_set() or bool(job.killed)
        bg_work._update(wid, status="stopped" if stopped else "done")
        text = (f"Background command stopped: {short}" if stopped else
                f"Background command finished (exit {code}): {short}")
        if job.killed and not ev.is_set():
            text += f" — {job.killed.removeprefix('stopped: ')}"
        bg_work._post(job.session_id, text)
        cmd_jobs._record_end(job, code, job.killed)
    except Exception:  # noqa: BLE001
        log.debug("promoted job watch failed", exc_info=True)
    finally:
        bg_work._unbind(wid)
        try:
            cmd_jobs._forget(job)       # closes it: frees the pane, the spool
        except Exception:  # noqa: BLE001
            log.debug("forget promoted job failed", exc_info=True)


def answer_suffix(jobs: list) -> str:
    """The line the answer ends with when jobs were promoted."""
    if not jobs:
        return ""
    names = ", ".join(
        f"`{(j.cmd or '').strip().splitlines()[0][:60] if (j.cmd or '').strip() else j.key}`"
        f" ({j.key})" for j in jobs[:4])
    more = f" and {len(jobs) - 4} more" if len(jobs) > 4 else ""
    return (f"\n\n_Still running in the background: {names}{more}. Its "
            "result will be posted in this chat when it finishes; Stop ends "
            "it._")


__all__ = ["answer_suffix", "promote", "promote_turn_jobs"]
