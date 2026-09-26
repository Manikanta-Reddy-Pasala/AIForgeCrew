"""A turn answers while a command it handed off is still running.

``cmd_jobs.end_turn`` kills a turn's handed-off jobs when the turn ends, so
an answer that says "the build is still running" would be wrong a moment
later. When the model answers anyway, the still-running jobs are PROMOTED to
explicit background jobs instead: they outlive the turn, Stop still ends them
(a ``bg_work`` row), and their outcome is posted to the chat when they finish
— the same as a command started with ``background: true``. The answer gets a
line saying so (:func:`answer_suffix`).
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
            promote(job)
            done.append(job)
        except Exception:  # noqa: BLE001
            log.debug("promote %s failed", getattr(job, "key", "?"),
                      exc_info=True)
    return done


def promote(job) -> None:
    """``job`` becomes an explicit background job (see the module doc)."""
    if job.explicit:
        return
    job.explicit = True
    opts = getattr(job, "watch_opts", None)
    if isinstance(opts, dict):
        # A spooled command already has a bg_work row and watcher: it only
        # stayed quiet because the agent was watching it. Now it reports.
        opts["announce"] = True
        return
    _watch_in_background(job)


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


def _watch(job, wid: int, ev: threading.Event) -> None:
    from aiforge_core.runtime import bg_work
    short = (job.cmd or "").strip().replace("\n", " ")[:80]
    try:
        while job.alive():
            if ev.is_set():
                job.kill("stopped: Stop")
                break
            time.sleep(_POLL_S)
        code = getattr(job.proc, "returncode", None)
        stopped = ev.is_set() or bool(job.killed)
        bg_work._update(wid, status="stopped" if stopped else "done")
        text = (f"Background command stopped: {short}" if stopped else
                f"Background command finished (exit {code}): {short}")
        bg_work._post(job.session_id, text)
        cmd_jobs._record_end(job, code, job.killed)
    except Exception:  # noqa: BLE001
        log.debug("promoted job watch failed", exc_info=True)
    finally:
        bg_work._unbind(wid)


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
