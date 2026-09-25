"""Tick loop: fire due jobs by creating tickets through the existing
pipeline. Runs as a daemon thread from the API startup hook (the
codebase's universal background-work pattern). Catch-up-once semantics
fall out of the due-query + recomputing next_run_at from *now* (not the
missed slot) — a 3-day backlog collapses to one fire.

Kill switch: AIFORGE_JOBS_DISABLE=1. Tick: AIFORGE_JOBS_TICK_S (30)."""
from __future__ import annotations

import logging
import os
import stat
import threading
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

# Agent jobs whose worker thread is STILL RUNNING, by job id. A job can reach
# its end time while its last run is mid-flight (fires 09:59, expires 10:00,
# takes ten minutes); the close must not delete the workspace out from under
# that thread. In-process only, which is exactly where the thread lives.
_RUNNING: set = set()
_RUNNING_LOCK = threading.Lock()


def is_running(job_id) -> bool:
    with _RUNNING_LOCK:
        return job_id in _RUNNING


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
    # An expired job must not fire, even when something reaches fire() directly
    # (run-now from the API, a tick racing the sweep). Closing here rather than
    # returning False keeps the row from lingering until the next sweep.
    exp = job.get("expires_at")
    if exp and exp <= now_s:
        from aiforge_core.jobs import lifecycle
        lifecycle.close_job(job, "reached its end time")
        return False
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
    fresh = store.get(job["id"]) or job
    return _dispatch(fresh)


def _dispatch(job: dict) -> bool:
    """Run a job whose slot is already claimed. Used by fire() and by the
    one startup retry of a run that died in flight."""
    kind = job.get("kind") or "ticket"
    if kind == "script":
        return _fire_script(job)
    if kind == "agent":
        return _fire_agent(job)
    try:
        from aiforge_core.tickets import store as tickets_mod
        t = tickets_mod.create(
            title=job["ticket_title"], body=job["ticket_body"],
            project=job.get("project"),
            metadata={"source": "scheduled_job", "job_id": job["id"]})
        ident = getattr(t, "identifier", None) or getattr(t, "id", "?")
        log.info("jobs.fired job=%s ticket=%s", job["id"], ident)
        from aiforge_core.jobs.outcome import post
        post(job, f"Scheduled “{job.get('name')}” filed ticket {ident}.")
        return True
    except Exception as exc:  # noqa: BLE001 — schedule already advanced
        # Slot is already consumed; record the error, do NOT re-fire.
        try:
            store.update(job["id"], last_error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        log.warning("jobs.fire_failed job=%s: %s", job["id"], exc)
        return False
    finally:
        if kind == "ticket":
            store.clear_run(job["id"], job.get("run_token"))


# Cancel events and live process groups, so closing a job stops the worker
# and not only the row. In-process: that is where the thread lives.
_STOP: dict = {}
_PROCS: dict = {}
_SESSION_AGENT: dict = {}


def request_stop(job_id) -> bool:
    """Ask the in-flight worker for this job to stop, and kill its processes.

    Returns True when something was actually running. A future job that has
    not started yet is only a row; closing the row is the caller's job.

    The stop Event is published before the worker thread starts. If it
    is already set, the worker exits immediately. ``run_status`` is
    always cleared so a restart cannot resume a run the user already
    stopped, even when this call beat the Event publish.
    """
    stopped = False
    inflight = False
    try:
        job = store.get(job_id)
    except Exception:  # noqa: BLE001
        job = None
    if job and job.get("run_status") == "running":
        inflight = True
    sid = _session_of(job) if job else None
    with _RUNNING_LOCK:
        ev = _STOP.get(job_id)
        procs = list(_PROCS.get(job_id) or ())
        bound = sid is not None and _SESSION_AGENT.get(sid) == job_id
    # A worker that has not published yet still exits: run_status is
    # cleared below and _claim_still_held() fails. Do not plant a set
    # Event here — a leftover would make the next claimed slot exit
    # the moment it binds.
    if ev is not None:
        ev.set()
        stopped = True
    if procs:
        from aiforge_core.runtime import proc_signals
        for pgid in procs:
            proc_signals.stop_group(pgid, pause_s=0.0)
        stopped = True
    if bound:
        try:
            from aiforge_core.runtime import chat_cancel, chat_runs
            if not chat_runs.is_running(sid):
                chat_cancel.cancel(sid)
                stopped = True
        except Exception:  # noqa: BLE001
            pass
    # Always drop the in-flight mark. Missing the Event used to leave
    # run_status=running, and resume_inflight started the work again.
    try:
        store.update(job_id, run_status=None, run_token=None,
                     run_attempt=0, run_pid=None)
    except Exception:  # noqa: BLE001
        pass
    return stopped or inflight


def request_stop_session(session_id) -> bool:
    """Stop the scheduled agent run bound to this chat, when there is one."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return False
    with _RUNNING_LOCK:
        job_id = _SESSION_AGENT.get(sid)
    if job_id is None:
        return False
    return request_stop(job_id)


def stop_all() -> int:
    """Stop every in-flight scheduled worker. Used by kill-all."""
    with _RUNNING_LOCK:
        ids = list(set(_STOP) | set(_RUNNING))
    n = 0
    for job_id in ids:
        if request_stop(job_id):
            n += 1
    return n


def running_agent_for_session(session_id):
    """Job id of the agent run this chat can steer, or None."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return None
    with _RUNNING_LOCK:
        return _SESSION_AGENT.get(sid)


def _session_of(job: dict):
    raw = job.get("session_id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _remember_proc(job_id, pid: int) -> None:
    if not pid:
        return
    with _RUNNING_LOCK:
        _PROCS.setdefault(job_id, set()).add(int(pid))


def _bind_stop(job_id):
    """The Event this job's worker watches. Reuse one a stop already planted."""
    with _RUNNING_LOCK:
        ev = _STOP.get(job_id)
        if ev is None:
            ev = threading.Event()
            _STOP[job_id] = ev
        return ev


def _claim_still_held(job: dict) -> bool:
    """False when request_stop already dropped this claimed slot.

    Direct dispatches (tests, a fire that has no token yet) have nothing
    to check and are still held."""
    token = job.get("run_token")
    if not token:
        return True
    try:
        row = store.get(job["id"])
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row.get("run_status") == "running"
                and row.get("run_token") == token)


def _still_this_job(job: dict) -> bool:
    """False when this id now names a different row or a later claim.

    Name+kind is not enough: a reclaimed slot keeps both and would let a
    late worker write ``last_error`` onto the new run. The token check
    is the same predicate as ``_claim_still_held``."""
    if not _claim_still_held(job):
        return False
    try:
        row = store.get(job["id"])
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row.get("name") == job.get("name")
                and (row.get("kind") or "ticket") == (job.get("kind") or "ticket"))


def _record_last_error(job: dict, last_error) -> None:
    """Write last_error only while this claimed run still owns the row."""
    if not _claim_still_held(job):
        return
    store.update_if_token(job["id"], job.get("run_token"), last_error=last_error)


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
    ev = _bind_stop(job["id"])

    def _run() -> None:
        def _on_start(proc) -> None:
            _remember_proc(job["id"], proc.pid)
            try:
                # start_new_session: pid is the process group.
                if _still_this_job(job):
                    store.update(job["id"], run_pid=proc.pid)
            except Exception:  # noqa: BLE001
                pass
            if ev.is_set():
                from aiforge_core.runtime import proc_signals
                proc_signals.stop_group(proc.pid, pause_s=0.0)

        if ev.is_set() or not _claim_still_held(job):
            with _RUNNING_LOCK:
                _STOP.pop(job["id"], None)
            store.clear_run(job["id"], job.get("run_token"))
            return
        try:
            try:
                res = scripts.run_script(path, on_start=_on_start)
            except Exception as exc:  # noqa: BLE001 — worker must never crash the thread
                res = {"ok": False, "error": str(exc)}
            from aiforge_core.jobs.outcome import post
            if res.get("ok"):
                try:
                    _record_last_error(job, None)
                except Exception:  # noqa: BLE001
                    pass
                log.info("jobs.fired script job=%s path=%s", job["id"], path)
                post(job, f"Scheduled “{job.get('name')}” finished.")
                return
            err = (res.get("error") or "script failed")
            # One line on the job chip. The chat note stays shorter than that.
            tail = (res.get("stderr") or res.get("stdout") or "").strip()
            msg = f"{err}: {tail}"[:500] if tail else err[:500]
            try:
                _record_last_error(job, msg)
            except Exception:  # noqa: BLE001
                pass
            log.warning("jobs.fire_script_failed job=%s: %s", job["id"], msg)
            first = (tail or err).splitlines()[0][:160]
            post(job, f"Scheduled “{job.get('name')}” failed: {first}")
        finally:
            with _RUNNING_LOCK:
                _STOP.pop(job["id"], None)
                _PROCS.pop(job["id"], None)
            store.clear_run(job["id"], job.get("run_token"))

    try:
        _t.Thread(target=_run, name=f"jobs-script-{job['id']}", daemon=True).start()
    except Exception:  # noqa: BLE001
        with _RUNNING_LOCK:
            _STOP.pop(job["id"], None)
        raise
    return True


def _job_workspace(job: dict) -> str:
    """Create (and return) the directory an agent job works in.

    The name is predictable and the parent is world-writable, so "make it if it
    isn't there" is not enough: another local account can pre-create
    ``/tmp/aiforge-job-<n>``, and ``exist_ok=True`` would happily hand the agent
    a directory somebody else owns — everything the run writes, readable and
    swappable by them. So the directory must be OURS: 0700, not a symlink, and
    owned by this uid. When it is not, the run gets a fresh private directory
    instead of refusing to work.
    """
    import tempfile

    from aiforge_core.jobs import lifecycle
    cwd = lifecycle.workspace_of(job)
    if cwd:
        try:
            os.makedirs(cwd, mode=0o700, exist_ok=True)
            st = os.lstat(cwd)
            if (stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
                    and st.st_uid == os.getuid()):
                os.chmod(cwd, 0o700)
                return cwd
            log.warning("jobs.workspace %s is not ours — using a private one", cwd)
        except OSError as exc:
            log.warning("jobs.workspace %s unusable (%s) — using a private one",
                        cwd, exc)
    return tempfile.mkdtemp(prefix=f"aiforge-job-{job.get('id')}-")


def _arm_agent_session(job: dict):
    """Publish the chat binding before the worker thread starts.

    A message that arrives in the gap after Thread.start() and before the
    worker runs must already fold into this job, not open a second turn.
    The cancel token is the object this job created. A later turn that
    replaces it is left alone when the job ends.
    Returns ``(session_id, token_or_None)``."""
    sid = _session_of(job)
    if sid is None:
        return None, None
    from aiforge_core.runtime import chat_cancel, chat_interject, chat_runs
    with _RUNNING_LOCK:
        _SESSION_AGENT[sid] = job["id"]
    token = None
    try:
        if not chat_runs.is_running(sid) and chat_cancel.get(sid) is None:
            token = chat_cancel.start(sid)
        chat_interject.set_steerable(sid, True)
    except Exception:  # noqa: BLE001
        pass
    return sid, token


def _disarm_agent_session(sid, job_id, token) -> None:
    if sid is None:
        return
    try:
        from aiforge_core.runtime import chat_cancel, chat_interject
        if token is not None and chat_cancel.finish_if_owner(sid, token):
            chat_interject.set_steerable(sid, False)
    except Exception:  # noqa: BLE001
        pass
    with _RUNNING_LOCK:
        if _SESSION_AGENT.get(sid) == job_id:
            _SESSION_AGENT.pop(sid, None)


def _run_agent_job(job: dict, prompt: str) -> None:
    """Execute one agent job's request through the chat agent (full tool surface).

    A job created from a chat runs IN that session so a later message can
    steer or stop it. A job from the Jobs page stays session-less. Records
    the outcome on ``last_error`` (None = ok) and, when there is a session,
    one short line in that chat. Never crashes the worker thread."""
    from aiforge_core.jobs.outcome import post
    sid = _session_of(job)
    try:
        from aiforge_core.runtime.chat_agent import run_chat_agent
        cwd = _job_workspace(job)
        final, err = "", None
        for ev in run_chat_agent([{"role": "user", "content": prompt}],
                                 cwd=cwd, role="chat", session_id=sid):
            etype = ev.get("type")
            if etype == "message":
                final = ev.get("text") or final
            elif etype == "error":
                err = ev.get("text")
        if err:
            _record_last_error(job, str(err)[:500])
            log.warning("jobs.fire_agent_failed job=%s: %s", job["id"], err)
            post(job, f"Scheduled “{job.get('name')}” stopped."
                 if "stopped" in str(err).lower()
                 else f"Scheduled “{job.get('name')}” failed: {str(err)[:160]}")
        else:
            _record_last_error(job, None)
            log.info("jobs.fired agent job=%s: %s", job["id"], (final or "")[:160])
            summary = (final or "finished").splitlines()[0][:240]
            post(job, f"Scheduled “{job.get('name')}”: {summary}")
    except Exception as exc:  # noqa: BLE001 — worker never crashes the thread
        try:
            _record_last_error(job, str(exc)[:500])
        except Exception:  # noqa: BLE001
            pass
        log.warning("jobs.fire_agent_crashed job=%s: %s", job["id"], exc)
        post(job, f"Scheduled “{job.get('name')}” failed: {str(exc)[:160]}")


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
        from aiforge_core.runtime.run_interrupt import bind_stop_event
        from aiforge_core.runtime import chat_cancel
        if ev.is_set() or not _claim_still_held(job):
            _disarm_agent_session(sid, job["id"], owned_token)
            with _RUNNING_LOCK:
                _RUNNING.discard(job["id"])
                _STOP.pop(job["id"], None)
            store.clear_run(job["id"], job.get("run_token"))
            return
        try:
            if sid is not None:
                chat_cancel.set_active(sid)
            bind_stop_event(ev)
            with keep_awake(f"job {job.get('id')}"):
                _run_agent_job(job, prompt)
        finally:
            bind_stop_event(None)
            _disarm_agent_session(sid, job["id"], owned_token)
            with _RUNNING_LOCK:
                _RUNNING.discard(job["id"])
                _STOP.pop(job["id"], None)
            store.clear_run(job["id"], job.get("run_token"))

    # A slow run must not overlap its own next firing: both would work in the
    # same job workspace. This firing's slot is already consumed, so it is
    # skipped (and said so on the job) rather than queued.
    with _RUNNING_LOCK:
        if job["id"] in _RUNNING:
            busy = True
        else:
            busy = False
            _RUNNING.add(job["id"])
    if busy:
        log.info("jobs.fire_agent skipped job=%s: previous run still running",
                 job["id"])
        try:
            store.update(job["id"], last_error="skipped: the previous run was "
                                               "still running at this firing")
        except Exception:  # noqa: BLE001
            pass
        # The slot was claimed, but no worker is going to clear it. Leaving
        # the row in-flight would make the next startup run it again on top
        # of the one that is still going.
        store.clear_run(job["id"], job.get("run_token"))
        return False
    ev = _bind_stop(job["id"])
    sid, owned_token = _arm_agent_session(job)
    try:
        _t.Thread(target=_run, name=f"jobs-agent-{job['id']}", daemon=True).start()
    except Exception:  # noqa: BLE001 — a thread that never started is not running
        with _RUNNING_LOCK:
            _RUNNING.discard(job["id"])
            _STOP.pop(job["id"], None)
        _disarm_agent_session(sid, job["id"], owned_token)
        raise
    return True



def tick(now: datetime | None = None) -> int:
    """Fire everything due. One job's failure never blocks the rest.
    Returns the number of SUCCESSFUL fires."""
    now = now or datetime.now()
    # Close what has ended BEFORE firing what is due: a job whose end time and
    # next slot both passed in the same tick should close, not get one last run.
    try:
        from aiforge_core.jobs import lifecycle
        lifecycle.close_expired(now)
    except Exception as exc:  # noqa: BLE001 — the sweep never blocks the fires
        log.warning("jobs.tick sweep crashed: %s", exc)
    fired = 0
    for job in store.due_jobs(now.isoformat(timespec="seconds")):
        try:
            if fire(job, now=now):
                fired += 1
        except Exception as exc:  # noqa: BLE001 — belt over fire()'s braces
            log.warning("jobs.tick job=%s crashed: %s", job.get("id"), exc)
    return fired


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _kill_pid(pid) -> None:
    if not pid:
        return
    try:
        from aiforge_core.runtime import proc_signals
        proc_signals.stop_group(int(pid), pause_s=0.0)
    except Exception:  # noqa: BLE001
        pass


def _watch_live_script(job: dict, ev, pid: int) -> None:
    """Wait out a script that survived restart. Do not launch another."""
    try:
        if ev.is_set() or not _claim_still_held(job):
            _kill_pid(pid)
            return
        while _pid_alive(pid):
            if ev.is_set():
                _kill_pid(pid)
                break
            time.sleep(0.5)
    finally:
        with _RUNNING_LOCK:
            _STOP.pop(job["id"], None)
            _PROCS.pop(job["id"], None)
        store.clear_run(job["id"], job.get("run_token"))


def resume_inflight() -> int:
    """Retry a claimed run that died with the process, once.

    A second restart finds ``run_attempt >= 2`` and marks the run stopped
    instead of looping. The next cron slot is untouched."""
    from aiforge_core.jobs.outcome import post
    n = 0
    try:
        rows = store.inflight_jobs()
    except Exception as exc:  # noqa: BLE001
        log.warning("jobs.resume query failed: %s", exc)
        return 0
    for job in rows:
        attempt = int(job.get("run_attempt") or 1)
        pid = job.get("run_pid")
        if (job.get("kind") == "script") and _pid_alive(pid):
            if attempt >= 2:
                _kill_pid(pid)
            else:
                # One process wins the reattach. The loser must not
                # dispatch a second copy of a script that is still alive.
                try:
                    won = store.bump_inflight(job["id"], attempt)
                except Exception:  # noqa: BLE001
                    won = False
                if not won:
                    continue
                ev = _bind_stop(job["id"])
                _remember_proc(job["id"], int(pid))
                threading.Thread(
                    target=_watch_live_script, name=f"jobs-reattach-{job['id']}",
                    daemon=True, args=(job, ev, int(pid))).start()
                continue
        if attempt >= 2:
            try:
                won = store.release_retried(
                    job["id"],
                    "stopped after restart; already retried once")
            except Exception:  # noqa: BLE001
                won = False
            if won:
                post(job, f"Scheduled “{job.get('name')}” stopped: the process "
                          "restarted and this run was already retried.")
            continue
        try:
            won = store.bump_inflight(job["id"], attempt)
        except Exception as exc:  # noqa: BLE001
            log.warning("jobs.resume mark failed job=%s: %s", job.get("id"), exc)
            continue
        if not won:
            continue
        fresh = store.get(job["id"]) or job
        try:
            _dispatch(fresh)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("jobs.resume job=%s: %s", job.get("id"), exc)
    return n


def run_loop() -> None:
    """Blocking loop for the daemon thread. Never raises."""
    if not _CRONITER_OK:
        log.warning("jobs.scheduler disabled — 'croniter' not installed "
                    "(run `uv pip install croniter` / `uv sync`)")
        return
    try:
        resume_inflight()
    except Exception as exc:  # noqa: BLE001 — resume must not kill the loop
        log.warning("jobs.resume crashed: %s", exc)
    log.info("jobs.scheduler loop started (tick=%ss)", _tick_s())
    while True:
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — the loop must survive
            log.warning("jobs.tick crashed: %s", exc)
        time.sleep(_tick_s())
