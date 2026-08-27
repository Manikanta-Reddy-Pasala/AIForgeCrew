"""When a scheduled loop ENDS — and what survives it.

A monitoring loop ("watch the error log", "check the deploy every 15 minutes")
is asked for in the middle of an incident and forgotten the moment the incident
is over. Nothing closed it: `schedule_task` wrote a cron row and the tick loop
fired it forever, so a box collected loops nobody remembered asking for, each
still filing tickets and running agents.

So every loop now carries an END. The user's own words decide it when they say
them ("until tomorrow", "for 3 days"); when they don't, the loop closes itself
after :data:`DEFAULT_TTL_MINUTES` — the deliberate default is two hours, long
enough to outlive the thing being watched, short enough that forgetting is
free. `until="forever"` opts out, and is the only way to get an immortal job.

What survives the close is the point: the LEARNING (a memory capture — what was
watched, how it ended, whether it was failing) and the SCRIPT (a script job's
file on disk is the operator's, never ours to delete). The job row, its cron and
its ticket body are scaffolding and go away with it.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from datetime import datetime, timedelta

log = logging.getLogger("aiforge.jobs")

_NEVER = ("forever", "never", "none", "no", "off", "0", "-1", "indefinite")

# now + this, when the caller names no end. Two hours, per the rule above.
_DEFAULT_TTL_ENV = "AIFORGE_JOB_DEFAULT_TTL_MINUTES"
# Ceiling on an explicit `until`, so "until 2099" is a typo, not a decade of
# cron fires. 30 days.
_MAX_TTL_ENV = "AIFORGE_JOB_MAX_TTL_MINUTES"

_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*"
                     r"(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|"
                     r"d|day|days|w|week|weeks)?\s*$", re.I)

_UNIT_MINUTES = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "w": 10080, "week": 10080, "weeks": 10080,
}


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def default_ttl_minutes() -> int:
    return _env_int(_DEFAULT_TTL_ENV, 120)


def max_ttl_minutes() -> int:
    return _env_int(_MAX_TTL_ENV, 30 * 24 * 60)


def _end_of(day: datetime) -> datetime:
    return day.replace(hour=23, minute=59, second=59, microsecond=0)


def _keyword_until(raw: str, now: datetime) -> datetime | None:
    """Calendar words. "until tomorrow" means THROUGH tomorrow — the end of that
    day, not this time tomorrow: someone who says it wants the loop alive while
    they look at it in the morning."""
    if raw in ("today", "tonight", "eod", "end of day"):
        return _end_of(now)
    if raw == "tomorrow":
        return _end_of(now + timedelta(days=1))
    if raw in ("this week", "week", "end of week"):
        return _end_of(now + timedelta(days=7 - now.isoweekday()))
    return None


def _parse_absolute(raw: str, now: datetime) -> datetime | None:
    """ISO datetime, or a bare ISO date (which means the END of that date)."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=None)
    # A bare date parses to midnight, which would expire the job before its
    # first fire on that day; "until 2026-09-01" means through the 1st.
    if len(raw) <= 10 and dt.time() == datetime.min.time():
        dt = _end_of(dt)
    return dt if dt > now else None


def parse_until(raw, *, now: datetime | None = None) -> tuple[str | None, str | None]:
    """``(expires_at_iso | None, error | None)`` for a caller's `until`.

    ``None``/empty → the default TTL (a job ALWAYS gets an end unless one is
    refused explicitly). ``"forever"`` → ``(None, None)``: no expiry, and the
    caller is on the hook for cancelling it. Accepted otherwise: a duration
    ("90m", "3h", "2d", "1w", a bare number of minutes), a calendar word
    ("today", "tomorrow", "this week"), or an ISO date / datetime.
    """
    now = now or datetime.now()
    txt = ("" if raw is None else str(raw)).strip().lower()
    if not txt:
        return (now + timedelta(minutes=default_ttl_minutes())
                ).isoformat(timespec="seconds"), None
    if txt in _NEVER:
        return None, None

    txt = re.sub(r"^(until|till|til|for|through|thru)\s+", "", txt).strip()
    end = _keyword_until(txt, now)
    if end is None:
        m = _DUR_RE.match(txt)
        if m:
            minutes = float(m.group(1)) * _UNIT_MINUTES.get(
                (m.group(2) or "m").lower(), 1)
            if minutes <= 0:
                return None, f"`until={raw!r}` is not a future time."
            # Clamp BEFORE the arithmetic. `until="99999999999999999999d"` is a
            # number timedelta cannot hold, and it arrived as a string from a
            # model or an API caller: unclamped it raised OverflowError out of
            # here, which is a 500 on POST /api/jobs and an exception in the
            # tool loop instead of the capped answer the next lines already
            # give every other over-long horizon.
            minutes = min(minutes, float(max_ttl_minutes()))
            end = now + timedelta(minutes=minutes)
        else:
            end = _parse_absolute(txt, now)
    if end is None:
        return None, (f"could not read `until={raw!r}` — use a duration "
                      "(90m, 3h, 2d), a day (today, tomorrow), an ISO date/"
                      "time, or 'forever' for a job that never self-closes.")
    if end <= now:
        return None, f"`until={raw!r}` is in the past."

    cap = now + timedelta(minutes=max_ttl_minutes())
    if end > cap:
        end = cap
    return end.isoformat(timespec="seconds"), None


# A job's scratch workspace (agent jobs run in tempdir/aiforge-job-<id>). Only
# these two kinds of file are worth keeping out of it — everything else there is
# a checkout, a log or a half-finished artefact of one run.
_KEEP_SUFFIXES = (".sh", ".py")
_KEEP_MAX_FILES = 10
_KEEP_MAX_BYTES = 256 * 1024


def workspace_of(job: dict) -> str:
    """Where an agent job runs — must match adk/jobs/scheduler._run_agent_job.

    The id is forced through ``int`` and the result is checked to still be a
    direct child of the temp dir. This path is handed to ``shutil.rmtree``: an
    id of ``../../something`` would otherwise make "clean up the workspace"
    delete a directory nobody asked about. Job ids come from a SQLite
    AUTOINCREMENT column today, so this is a bound on tomorrow, not a bug
    report — but the cost of the bound is one int() and the cost of not having
    it is somebody's tree.
    """
    import tempfile
    try:
        job_id = int(job.get("id"))
    except (TypeError, ValueError):
        return ""
    root = os.path.realpath(tempfile.gettempdir())
    path = os.path.join(root, f"aiforge-job-{job_id}")
    return path if os.path.dirname(path) == root else ""


def _is_in_flight(job: dict) -> bool:
    """True while an agent job's worker thread still owns its workspace.

    Closing the row mid-run is fine; pulling the directory out from under a
    working agent is not. The next sweep finds nothing to do here, so the
    leftover is bounded by one run.
    """
    try:
        from aiforge_core.jobs import scheduler
        return bool(scheduler.is_running(job.get("id")))
    except Exception:  # noqa: BLE001 — never block the close
        return False


def _worth_keeping(src: str, name: str) -> bool:
    """A file is worth keeping iff it is a script we can vouch for: right
    suffix, not a symlink, small enough to be source rather than an artefact.

    The symlink test is repeated at open time in :func:`_keep_one` — checking
    here only tells us what the name pointed at a moment ago.
    """
    if not name.endswith(_KEEP_SUFFIXES) or os.path.islink(src):
        return False
    try:
        return os.path.getsize(src) <= _KEEP_MAX_BYTES
    except OSError:
        return False


def _usable_workspace(job: dict) -> str:
    """The job's workspace, or "" when it is not one we may touch.

    The workspace lives in the shared temp dir under a PREDICTABLE name, which
    is the setup for the oldest trick there is: anyone who can write /tmp
    pre-creates ``aiforge-job-<n>`` as a symlink to a directory of their
    choosing, and this pass — which follows the name to copy scripts out and
    then deletes it — becomes their exfiltration and their rm. ``isdir()``
    follows symlinks, so the check has to be ``islink`` on the path itself
    plus a realpath that still lands where we expect.
    """
    ws = workspace_of(job)
    if not ws or os.path.islink(ws) or not os.path.isdir(ws):
        return ""
    if os.path.realpath(ws) != ws:
        log.warning("jobs.close workspace is not what it claims to be: %s", ws)
        return ""
    return ws


def _scripts_in(ws: str) -> list:
    """Script paths in the workspace, top level plus one directory down — the
    depth where "the script it wrote" lives. Deeper is a checkout, and walking
    a whole clone is not the job of a cleanup pass."""
    found = []
    for root, dirs, files in os.walk(ws):
        if os.path.relpath(root, ws).count(os.sep) >= 1:
            dirs[:] = []
        for name in sorted(files):
            if len(found) >= _KEEP_MAX_FILES:
                return found
            src = os.path.join(root, name)
            if _worth_keeping(src, name):
                found.append(src)
    return found


def _keep_one(src: str, job: dict, dest_dir: str, slug: str) -> str | None:
    """Copy one script into the jobs dir; return where it landed.

    Opened with ``O_NOFOLLOW`` and copied from that descriptor, so the symlink
    test in :func:`_worth_keeping` cannot be raced: between listing the
    workspace and reading it, whatever wrote those files could swap one for a
    link at any path this user can read, and a plain ``copy2`` would follow it
    and file the result as a script the job "wrote". The size is re-checked on
    the same descriptor for the same reason.
    """
    dest = os.path.join(dest_dir,
                        f"job-{job.get('id')}-{slug}-{os.path.basename(src)}")
    fd = None
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _KEEP_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb") as fsrc:
            fd = None  # fdopen owns it now
            # 0o700, not 0o755: these land in the user's own ~/.aiforge/jobs and
            # are run by this user's scheduler. Nothing else on the box needs to
            # read — let alone execute — a script an agent wrote unattended.
            # O_EXCL: never write THROUGH an existing name (a symlink planted in
            # the destination would otherwise redirect the write).
            dfd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(dfd, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, length=64 * 1024)
        return dest
    except OSError as exc:
        log.warning("jobs.close keep-script failed %s: %s", src, exc)
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _harvest_scripts(job: dict) -> list:
    """Move the SCRIPTS out of a closing job's workspace, then bin the rest.

    "Keep the learning and the scripts" is the whole cleanup rule: a loop that
    wrote a working check script leaves that script behind (in the jobs dir,
    where a script job can actually be pointed at it), and leaves nothing else —
    no half-cloned repos, no logs, no temp checkouts filling /tmp for weeks.
    """
    ws = _usable_workspace(job)
    if not ws or _is_in_flight(job):
        return []
    kept = []
    try:
        from aiforge_core.jobs import scripts as jobs_scripts
        dest_dir = jobs_scripts.jobs_dir()
        slug = jobs_scripts.slugify(job.get("name") or "job")
        kept = [d for d in (_keep_one(src, job, dest_dir, slug)
                            for src in _scripts_in(ws)) if d]
    except Exception as exc:  # noqa: BLE001 — cleanup never blocks the close
        log.warning("jobs.close harvest failed job=%s: %s", job.get("id"), exc)
    shutil.rmtree(ws, ignore_errors=True)
    return kept


def _learning_text(job: dict, reason: str, kept: list | None = None) -> str:
    """What the loop was, and how it ended. This is the ONLY thing that outlives
    the row, so it carries the instruction (what we were watching for) and the
    outcome — not the cron plumbing."""
    body = (job.get("ticket_body") or "").strip()
    lines = [
        f"Scheduled {job.get('kind') or 'ticket'} job "
        f"{job.get('name')!r} closed: {reason}.",
        f"Schedule: {job.get('cron')}. "
        f"Created {job.get('created_at')}, last run {job.get('last_run_at') or 'never'}.",
    ]
    if body:
        lines.append(f"It ran: {body[:600]}")
    if job.get("script_path"):
        # The script is the second thing that survives — name it so the next
        # session can find it instead of writing it again.
        lines.append(f"Script kept at: {job['script_path']}")
    for path in (kept or []):
        lines.append(f"Script kept at: {path}")
    if job.get("last_error"):
        lines.append("It was FAILING when it closed — the same watch set up "
                     "again should expect that failure, not a clean start.")
    return "\n".join(lines)


def _capture_learning(job: dict, reason: str, kept: list | None = None) -> bool:
    """Write the learning. Never raises: a memory that is down must not strand a
    job row forever — the close matters more than the note."""
    try:
        from aiforge_core.memory.md_store import capture
        capture("learning", _learning_text(job, reason, kept),
                title=f"scheduled job closed: {job.get('name')}"[:70],
                topic="scheduled-jobs", source="job_close",
                tags=["scheduled-job", f"job-kind:{job.get('kind') or 'ticket'}"])
        return True
    except Exception as exc:  # noqa: BLE001 — never block the close
        log.warning("jobs.close learning capture failed job=%s: %s",
                    job.get("id"), exc)
        return False


def close_job(job: dict, reason: str = "expired") -> dict:
    """End a job: keep the learning, keep the script, drop the row.

    Deliberately NOT `enabled=0`: a disabled row is a loop nobody closed, which
    is the mess this exists to prevent — it stays in every list, and the next
    person has to work out whether it is dead or paused. The script file on disk
    is the operator's and is left exactly where it is.
    """
    from aiforge_core.jobs import store

    # Scripts out of the workspace FIRST, so the learning can name where they
    # went; the workspace itself goes with them.
    kept = _harvest_scripts(job)
    learned = _capture_learning(job, reason, kept)
    deleted = False
    try:
        deleted = store.delete(job["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs.close delete failed job=%s: %s", job.get("id"), exc)
    log.info("jobs.closed job=%s name=%r reason=%s learning=%s",
             job.get("id"), job.get("name"), reason, learned)
    return {"ok": deleted, "job_id": job.get("id"), "name": job.get("name"),
            "reason": reason, "learning_captured": learned,
            "script_kept": job.get("script_path") or None,
            "scripts_kept": kept, "workspace_removed": True}


def close_expired(now: datetime | None = None) -> int:
    """Close every job whose end has passed. Returns how many were closed."""
    from aiforge_core.jobs import store

    now = now or datetime.now()
    closed = 0
    try:
        expired = store.expired_jobs(now.isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001 — a broken sweep must not stop the tick
        log.warning("jobs.close_expired query failed: %s", exc)
        return 0
    for job in expired:
        try:
            if close_job(job, "reached its end time")["ok"]:
                closed += 1
        except Exception as exc:  # noqa: BLE001 — one bad row never blocks the rest
            log.warning("jobs.close_expired job=%s crashed: %s",
                        job.get("id"), exc)
    return closed


__all__ = ["parse_until", "close_job", "close_expired",
           "default_ttl_minutes", "max_ttl_minutes"]
