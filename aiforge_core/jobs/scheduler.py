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

from croniter import croniter

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
    """Create the job's ticket and advance its schedule. Fire failure is
    soft-but-visible: last_error recorded on the row (UI chip), schedule
    STILL advances so a broken fire can't hot-loop every tick. Returns
    True on a successful fire."""
    now = now or datetime.now()
    now_s = now.isoformat(timespec="seconds")
    nxt = croniter(job["cron"], now).get_next(datetime) \
        .isoformat(timespec="seconds")
    try:
        from aiforge_core.tickets import store as tickets_mod
        # Accepted race: an API run-now can overlap a tick (no per-job
        # lock) and store flakiness between create+mark_fired can, in
        # theory, double-fire. Both are rare and tolerable for the
        # review-gated ticket runs jobs produce; revisit with a
        # compare-and-swap advance if a non-idempotent job type appears.
        t = tickets_mod.create(
            title=job["ticket_title"], body=job["ticket_body"],
            project=job.get("project"),
            metadata={"source": "scheduled_job", "job_id": job["id"]})
        store.mark_fired(job["id"], last_run_at=now_s, next_run_at=nxt)
        log.info("jobs.fired job=%s ticket=%s", job["id"],
                 getattr(t, "identifier", getattr(t, "id", "?")))
        return True
    except Exception as exc:  # noqa: BLE001 — record + advance, never raise
        store.mark_fired(job["id"], last_run_at=now_s, next_run_at=nxt,
                         last_error=str(exc)[:500])
        log.warning("jobs.fire_failed job=%s: %s", job["id"], exc)
        return False


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
    log.info("jobs.scheduler loop started (tick=%ss)", _tick_s())
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("jobs.tick crashed: %s", exc)
        time.sleep(_tick_s())
