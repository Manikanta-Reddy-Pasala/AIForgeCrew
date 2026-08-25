"""Tick loop: fire due jobs by creating tickets through the existing
pipeline. Runs as a daemon thread from the API startup hook (the
codebase's universal background-work pattern). Catch-up-once semantics
fall out of the due-query + recomputing next_run_at from *now* (not the
missed slot) — a 3-day backlog collapses to one fire.

Kill switch: AIFORGE_JOBS_DISABLE=1. Tick: AIFORGE_JOBS_TICK_S (30)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

try:
    from croniter import croniter
    _CRONITER_OK = True
except ImportError:  # pragma: no cover — dep missing → scheduler no-ops
    croniter = None  # type: ignore
    _CRONITER_OK = False

from aiforge_core.jobs import store

log = logging.getLogger("aiforge.jobs")


def _tick_s() -> int:
    try:
        return max(5, int(os.environ.get("AIFORGE_JOBS_TICK_S", "30")))
    except (TypeError, ValueError):
        return 30


def _disabled() -> bool:
    return os.environ.get("AIFORGE_JOBS_DISABLE", "").strip().lower() \
        in ("1", "true")


def fire(job: dict, *, now: datetime | None = None) -> bool:
    """Advance the job's schedule, THEN create its ticket. Returns True on
    a successful ticket create.

    Ordering is deliberate — advance-then-fire gives AT-MOST-ONCE: if the
    ticket create fails, the slot is already consumed so the next tick
    cannot re-fire it (a transient failure skips one run rather than
    duplicating tickets/PRs; catch-up-once handles the next slot). The
    inverse (create-then-advance) risks a duplicate ticket every tick when
    the advance write fails after the create succeeds.

    Concurrency: the advance is an atomic compare-and-swap claim
    (``store.claim`` — conditional UPDATE on the current slot), so a run-now
    overlapping a tick, or a second replica, loses the race and does NOT fire.
    No double ticket/PR.

    Fire failure is soft-but-visible: last_error is recorded on the row
    (UI chip). Never raises."""
    now = now or datetime.now()
    now_s = now.isoformat(timespec="seconds")
    # Compute the next slot defensively — an impossible-date cron
    # (e.g. "0 0 31 2 *") passes croniter.is_valid at save time but raises
    # here; disable such a job rather than crash the tick every 30s.
    try:
        nxt = croniter(job["cron"], now).get_next(datetime) \
            .isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 — unschedulable cron
        log.warning("jobs.fire unschedulable cron job=%s cron=%r: %s",
                    job["id"], job.get("cron"), exc)
        try:
            store.update(job["id"], enabled=False,
                         last_error=f"unschedulable cron: {exc}"[:500])
        except Exception:  # noqa: BLE001
            pass
        return False
    # CLAIM the slot atomically FIRST (at-most-once). The conditional advance
    # only succeeds if the row is still at THIS slot — so a run-now overlapping
    # the tick (or a second replica) loses the race and returns without firing,
    # instead of both creating a ticket. If the write fails we have NOT created a
    # ticket yet, so skipping is safe — no duplicate.
    try:
        claimed = store.claim(job["id"], expected_next_run_at=job["next_run_at"],
                              last_run_at=now_s, next_run_at=nxt)
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs.fire advance failed job=%s: %s", job["id"], exc)
        return False
    if not claimed:
        log.info("jobs.fire slot already claimed job=%s — skipping (no double-fire)",
                 job["id"])
        return False
    _kind = job.get("kind") or "ticket"
    if _kind == "script":
        return _fire_script(job)
    if _kind == "agent":
        return _fire_agent(job)
    try:
        from aiforge_core.tickets import store as tickets_mod
        t = tickets_mod.create(
            title=job["ticket_title"], body=job["ticket_body"],
            project=job.get("project"),
            metadata={"source": "scheduled_job", "job_id": job["id"]})
        log.info("jobs.fired job=%s ticket=%s", job["id"],
                 getattr(t, "identifier", getattr(t, "id", "?")))
        return True
    except Exception as exc:  # noqa: BLE001 — schedule already advanced
        # Slot is already consumed; record the error, do NOT re-fire.
        try:
            store.update(job["id"], last_error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        log.warning("jobs.fire_failed job=%s: %s", job["id"], exc)
        return False


def _fire_script(job: dict) -> bool:
    """Launch a script job's local script ASYNC (schedule already advanced), so a
    slow/hung script (up to the 900s timeout) can NEVER block the single-threaded
    tick loop — which would stall every other due job — nor the run-now HTTP
    request. The worker records exit code on ``last_error`` (UI chip) when it
    finishes. Returns True = dispatched (deterministic ops failures stay
    visible-but-soft; the launch itself doesn't raise)."""
    import threading as _t

    from aiforge_core.jobs import scripts
    path = job.get("script_path") or ""

    def _run() -> None:
        try:
            res = scripts.run_script(path)
        except Exception as exc:  # noqa: BLE001 — worker must never crash the thread
            res = {"ok": False, "error": str(exc)}
        if res.get("ok"):
            try:
                store.update(job["id"], last_error=None)
            except Exception:  # noqa: BLE001
                pass
            log.info("jobs.fired script job=%s path=%s", job["id"], path)
            return
        err = (res.get("error") or "script failed")
        tail = (res.get("stderr") or res.get("stdout") or "").strip()
        msg = f"{err}: {tail}"[:500] if tail else err[:500]
        try:
            store.update(job["id"], last_error=msg)
        except Exception:  # noqa: BLE001
            pass
        log.warning("jobs.fire_script_failed job=%s: %s", job["id"], msg)

    _t.Thread(target=_run, name=f"jobs-script-{job['id']}", daemon=True).start()
    return True


def _run_agent_job(job: dict, prompt: str) -> None:
    """Execute one agent job's request through the chat agent (full tool surface,
    autonomous — session_id=None, no approval gate). Records the outcome on
    ``last_error`` (None = ok). Never crashes the worker thread."""
    try:
        import os
        import tempfile
        from aiforge_core.runtime.chat_agent import run_chat_agent
        cwd = os.path.join(tempfile.gettempdir(), f"aiforge-job-{job['id']}")
        os.makedirs(cwd, exist_ok=True)
        final, err = "", None
        for ev in run_chat_agent([{"role": "user", "content": prompt}],
                                 cwd=cwd, role="chat", session_id=None):
            etype = ev.get("type")
            if etype == "message":
                final = ev.get("text") or final
            elif etype == "error":
                err = ev.get("text")
        if err:
            store.update(job["id"], last_error=str(err)[:500])
            log.warning("jobs.fire_agent_failed job=%s: %s", job["id"], err)
        else:
            store.update(job["id"], last_error=None)
            log.info("jobs.fired agent job=%s: %s", job["id"], (final or "")[:160])
    except Exception as exc:  # noqa: BLE001 — worker never crashes the thread
        try:
            store.update(job["id"], last_error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        log.warning("jobs.fire_agent_crashed job=%s: %s", job["id"], exc)


def _fire_agent(job: dict) -> bool:
    """Run an AGENT job: execute the job's request through the CHAT AGENT — a
    single agent with the FULL tool surface (jira/confluence/email + files/shell),
    NOT the code pipeline — so an operational task ("read JIRA-123 + the linked
    Confluence page, summarise, email me") actually reads the data and sends the
    mail, exactly like chat mode. Runs on a daemon thread (schedule already
    advanced) so a long run can't stall the tick loop. Returns True = dispatched."""
    import threading as _t
    prompt = (job.get("ticket_body") or job.get("ticket_title") or "").strip()

    def _run() -> None:
        # Hold the machine awake for the run. A scheduled job is the case that
        # needs it most: it fires while nobody is at the keyboard, so the box is
        # idling toward sleep the whole time it works. Screen lock is untouched.
        from aiforge_core.runtime.keep_awake import keep_awake
        with keep_awake(f"job {job.get('id')}"):
            _run_agent_job(job, prompt)

    _t.Thread(target=_run, name=f"jobs-agent-{job['id']}", daemon=True).start()
    return True



def tick(now: datetime | None = None) -> int:
    """Fire everything due. One job's failure never blocks the rest.
    Returns the number of SUCCESSFUL fires."""
    now = now or datetime.now()
    fired = 0
    for job in store.due_jobs(now.isoformat(timespec="seconds")):
        try:
            if fire(job, now=now):
                fired += 1
        except Exception as exc:  # noqa: BLE001 — belt over fire()'s braces
            log.warning("jobs.tick job=%s crashed: %s", job.get("id"), exc)
    return fired


def run_loop() -> None:
    """Blocking loop for the daemon thread. Never raises."""
    if not _CRONITER_OK:
        log.warning("jobs.scheduler disabled — 'croniter' not installed "
                    "(run `uv pip install croniter` / `uv sync`)")
        return
    log.info("jobs.scheduler loop started (tick=%ss)", _tick_s())
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("jobs.tick crashed: %s", exc)
        time.sleep(_tick_s())
