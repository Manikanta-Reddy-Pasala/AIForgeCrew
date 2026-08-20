"""ONE engine for internal recurring maintenance tasks (daily reindex, chat-md
compaction, graph dedup, …).

Before this there were several ad-hoc `while True: sleep(); do_thing()` loops,
each with its own error handling and no shared "fire at most once per slot"
logic. Register a task here and it runs on the single periodic loop, launched
through `background.spawn` (name + error sink) with a per-task debounce so a
manual trigger + the scheduled fire don't double-run.

    periodic.register("daily-reindex", _spawn_reindex_all, at_hour=3)
    periodic.register("chat-compact", compact_chats, every_s=6*3600)

``at_hour`` is AT-OR-AFTER, once per local day. A task pinned to the exact
instant would be skipped whenever a sleep overshoots it (sleep never returns
early), and never run at all on a machine asleep at the hour. So:

* fires the first time the loop wakes at/after the hour on a day it has not run
* a missed slot (yesterday's hour came and went with no run) makes it due at
  the next wake, whatever the hour — then it drifts back to its hour on the next day it is up
* ``strict_hour=True`` turns that catch-up OFF *before* the hour: a missed slot
  still runs at the next wake, but never earlier in the day than ``at_hour``.
  For a task the operator scheduled to stay out of working hours (the evening
  memory compaction), catching up at 09:00 is the very thing they asked against
* never twice within ``_MIN_GAP_S``; a run that RAISES retries after
  ``_RETRY_S``, at most ``_MAX_FAILS`` times a day
* the record (last attempt, whether it finished, retries, first-seen) lives in
  ``<AIFORGE_CONFIG_DIR>/periodic_state.json``, so restarts neither re-run a
  finished pass nor buy a failing one extra attempts

Not a distributed scheduler (that's `jobs/scheduler.py` for user cron jobs, with
`store.claim` at-most-once). This is in-process maintenance; for multi-replica
add a claim-key later. Off entirely with AIFORGE_PERIODIC_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("aiforge.periodic")

# A daily task that comes due at process start (its hour already passed, or a
# day was missed) waits this long first, plus a per-task spread, so the heavy
# jobs don't all land in the same second as the user's first request.
# Kept small: the delay is dead time for a short-lived process, and one that
# dies before grace+spread elapses would never run its daily tasks at all.
_GRACE_S = 30.0
_SPREAD_S = 90.0
_BOOT = time.monotonic()
# Two fires of the same daily task must be at least this far apart — the day
# marker alone lets an ``at_hour=0`` task fire at 23:30 (hour already "past")
# and again 30 minutes later at 00:00.
_MIN_GAP_S = 12 * 3600.0
# A daily task whose run RAISED becomes due again after this, instead of
# waiting a full day — but not immediately, or a failing heavy task would spin.
_RETRY_S = 3600.0
# …and only this many retries per day. Without the cap a task that fails every
# time (one bad brief in the memory fold) turns "once a day" back into hourly —
# exactly the cadence the daily schedule exists to remove.
_MAX_FAILS = 2


# _save_entry read-modify-writes a SHARED file, and the failure record is
# written from a worker thread while the loop thread records other tasks — an
# unlocked write clobbers exactly the ok=False record retries depend on.
_STATE_LOCK = threading.Lock()


def _state_path() -> Path:
    """Where the last-fired day of each daily task is remembered."""
    d = Path(os.path.expanduser(
        os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")))
    return d / "periodic_state.json"


def _load_state() -> dict:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        # Not silent: an unreadable file loses EVERY task's last-run day, so all
        # of them fire once more. Move it aside so the next write starts clean
        # instead of re-reporting the same corruption every cycle.
        log.warning("periodic state unreadable (%s) — starting fresh: %s",
                    path, exc)
        try:
            path.rename(path.with_suffix(".corrupt"))
        except Exception:  # noqa: BLE001
            pass
        return {}


def _save_entry(name: str, entry: dict) -> None:
    """Persist one task's run record. Best-effort.

    In-process state alone would re-run a daily task on EVERY restart that
    lands after its hour — with an LLM-heavy task (memory compaction) a crash
    loop then costs a full fold per restart. The FAILED attempt is recorded too
    (``ok: false`` + a retry count), so the backoff survives a restart instead
    of every restart buying another attempt.
    """
    try:
        with _STATE_LOCK:
            st = _load_state()
            st[name] = entry
            path = _state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            from aiforge_core.config import _atomic
            _atomic.write_text(path, json.dumps(st, indent=2, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 — never break the loop over state
        # WARNING, not debug: with no state on disk a restart loop re-runs the
        # daily (LLM-heavy) task every time — the exact thing this prevents.
        log.warning("periodic state write failed (%s): %s", _state_path(), exc)


def _parse_ts(raw) -> "datetime | None":
    """Local-naive datetime from a state value, or None. Never raises.

    Naive on purpose: every comparison here is against ``datetime.now()``, and
    one tz-aware stamp in the file would otherwise raise out of the loop thread
    and stop ALL maintenance for the life of the process.
    """
    if not isinstance(raw, str) or not raw:
        return None
    for parse in (datetime.fromisoformat,
                  lambda v: datetime.combine(date.fromisoformat(v[:10]),
                                             datetime.min.time())):
        try:
            out = parse(raw)
            # INSIDE the try: the UTC→local conversion of a stamp near the
            # datetime bounds raises OverflowError, and a task whose record
            # can't be read is never due again (the JSON is valid, so the
            # corrupt-file rename never fires and _persist is never reached).
            return out.astimezone().replace(tzinfo=None) if out.tzinfo else out
        except Exception:  # noqa: BLE001
            continue
    return None


@dataclass
class _Task:
    name: str
    fn: "Callable[[], object]"
    every_s: "float | None" = None      # fixed interval
    at_hour: "int | None" = None        # daily at-or-after local hour [0-23]
    # NEVER run before at_hour, not even to catch up a missed day. The task
    # simply waits for today's slot instead.
    strict_hour: bool = False
    debounce_s: float = 60.0
    _last: list = field(default_factory=lambda: [0.0])   # monotonic ts
    _rec: list = field(default_factory=lambda: [None])   # run record (see _record)
    _hold: list = field(default_factory=lambda: [0.0])   # monotonic: not due before
    _busy: list = field(default_factory=lambda: [False])  # a run is in flight

    def _record(self, now_dt: datetime) -> dict:
        """This task's run record — process state, else disk, else fresh.

        ``{"at": last attempt, "ok": did it finish, "fails": attempts today,
        "seen": when this task was first known}``. ``seen`` is PERSISTED: the
        missed-day catch-up measures from it when a task has never run, and an
        in-process-only baseline resets on restart — which on a machine that is
        never up at the hour means the task never becomes due at all.
        """
        if self._rec[0] is None:
            raw = _load_state().get(self.name)
            if isinstance(raw, str):                  # older state: a bare stamp
                raw = {"at": raw, "ok": True, "fails": 0}
            rec = raw if isinstance(raw, dict) else {}
            seen = _parse_ts(rec.get("seen")) or _parse_ts(rec.get("at")) or now_dt
            try:                              # json maps 1e400 → inf; int(inf)
                fails = int(rec.get("fails") or 0)    # raises OverflowError
            except Exception:  # noqa: BLE001 — hand-edited / wrong-typed value
                fails = 0
            self._rec[0] = {"at": _parse_ts(rec.get("at")),
                            "ok": bool(rec.get("ok", True)),
                            "fails": max(0, fails),
                            "seen": seen}
            if raw is None:      # first sighting — anchor the catch-up baseline
                self._persist()
        return self._rec[0]

    def _persist(self) -> None:
        r = self._rec[0] or {}
        _save_entry(self.name, {
            "at": r["at"].isoformat(timespec="seconds") if r.get("at") else "",
            "ok": bool(r.get("ok", True)),
            "fails": int(r.get("fails") or 0),
            "seen": (r.get("seen") or datetime.now()).isoformat(timespec="seconds"),
        })

    def _spread(self) -> float:
        """Deterministic per-task offset so daily tasks that come due together
        (typically at process start) don't all fire in the same second."""
        return (zlib.crc32(self.name.encode()) % int(_SPREAD_S)) if _SPREAD_S else 0.0

    def _delay(self, now_mono: float) -> float:
        """A due daily task still waits out the boot grace + its own spread."""
        uptime = max(0.0, now_mono - _BOOT)     # a fake/zero clock is "just booted"
        grace = max(0.0, (_GRACE_S + self._spread()) - uptime)
        return max(0.0, self._hold[0] - now_mono, grace)

    def _next_after(self, now_mono: float, now_dt: datetime) -> float:
        """Seconds until this task is next due (from now)."""
        if self.at_hour is not None:
            return self._daily_next(now_mono, now_dt)
        if self.every_s:
            elapsed = now_mono - self._last[0]
            return max(0.0, self.every_s - elapsed)
        return float("inf")

    def _daily_next(self, now_mono: float, now_dt: datetime) -> float:
        """AT-OR-AFTER the hour, once per local day — not "the next :00".

        time.sleep only ever overshoots, so a task pinned to the exact instant
        is skipped for a whole day whenever the loop wakes a few ms late.
        """
        rec = self._record(now_dt)
        last, ok, fails = rec["at"], rec["ok"], rec["fails"]
        if last is not None and last > now_dt + timedelta(minutes=5):
            # A stamp in the FUTURE (dead RTC, restored snapshot, clock moved
            # back) is CLAMPED to now, not dropped: dropping it also drops the
            # _MIN_GAP_S floor, and the pass runs a second time the same day.
            log.warning("periodic %s: run record is in the future (%s) — clamping",
                        self.name, last)
            last = rec["at"] = now_dt
            self._persist()
        gap = (now_dt - last).total_seconds() if last is not None else None

        if last is not None and not ok and fails < _MAX_FAILS \
                and last.date() == now_dt.date() \
                and not (self.strict_hour and now_dt.hour < self.at_hour):
            # A failed pass retries within the hour, then gives up until its
            # next slot. SAME DAY only: a retry that crossed midnight would eat
            # the new day's slot and walk the task around the clock. A
            # strict_hour task never retries BEFORE its hour either — a state
            # file written by an older build (or a clock change) can hold a
            # failed morning attempt, and retrying it is the same intrusion the
            # flag exists to stop.
            return self._delay(now_mono) if gap >= _RETRY_S else _RETRY_S - gap

        if last is not None and (last.date() == now_dt.date() or gap < _MIN_GAP_S):
            return self._wait_for_hour(now_mono, now_dt)    # already ran / too soon
        if now_dt.hour >= self.at_hour:
            return self._delay(now_mono)
        # MISSED-SLOT CATCH-UP. Measured against YESTERDAY's slot, not "24h ago":
        # an elapsed-time rule fires one wake later each day (the loop only
        # re-checks hourly), so the run walks forward and falls out of a laptop's
        # awake window entirely. A machine never up at the hour settles into one
        # run a day at its first wake instead.
        prev_slot = (now_dt - timedelta(days=1)).replace(
            hour=self.at_hour, minute=0, second=0, microsecond=0)
        if (last or rec["seen"]) < prev_slot and not self.strict_hour:
            return self._delay(now_mono)
        # strict_hour: a missed day does NOT buy a run at 09:00. The operator
        # picked the hour to keep this work out of their day; the pass waits
        # for today's slot (and the machine only has to be up at/after it).
        return self._wait_for_hour(now_mono, now_dt)

    def _wait_for_hour(self, now_mono: float, now_dt: datetime) -> float:
        nxt = now_dt.replace(hour=self.at_hour, minute=0, second=0, microsecond=0)
        if nxt <= now_dt:
            nxt += timedelta(days=1)
        return max((nxt - now_dt).total_seconds(), self._hold[0] - now_mono)


_TASKS: "list[_Task]" = []
_started = False
_lock = threading.Lock()


def register(name: str, fn: "Callable[[], object]", *,
             every_s: "float | None" = None, at_hour: "int | None" = None,
             strict_hour: bool = False, debounce_s: float = 60.0) -> None:
    """Register a recurring task. Give EITHER ``every_s`` OR ``at_hour``.

    ``strict_hour`` (at_hour only): never fire before the hour, even when a day
    was missed — the missed-slot catch-up then waits for today's slot instead of
    running at the next wake."""
    with _lock:
        if any(t.name == name for t in _TASKS):
            return
        _TASKS.append(_Task(name=name, fn=fn, every_s=every_s, at_hour=at_hour,
                            strict_hour=strict_hour, debounce_s=debounce_s))


def _fire(t: _Task, now_dt: "datetime | None" = None) -> None:
    now = time.monotonic()
    if now - t._last[0] < t.debounce_s:
        return
    if t._busy[0]:
        # A run slower than its own period (the first memory fold after an
        # upgrade can be) must not be re-entered by the next slot. Stamp _last
        # anyway: without it an every_s task stays due at 0.0, so the loop wakes
        # on its 1s floor and logs this line ~86k times a day.
        t._last[0] = now
        log.info("periodic %s still running — skipping this slot", t.name)
        return
    t._last[0] = now
    inner = t.fn
    if t.at_hour is not None:
        # Recorded BEFORE the work runs (the run is slow and off-thread), so a
        # restart mid-run can't double-fire it. ``now_dt`` comes from the loop's
        # own clock read so a wake at 23:59:59.99x can't book the run against
        # tomorrow and skip it. A run that RAISES is recorded as a FAILED
        # attempt: due again after _RETRY_S, at most _MAX_FAILS times a day.
        stamp = now_dt or datetime.now()
        rec = t._record(stamp)
        same_day = rec["at"] is not None and rec["at"].date() == stamp.date()
        rec.update(at=stamp, ok=True, fails=rec["fails"] if same_day else 0)
        t._persist()

        def inner(_fn=t.fn, _t=t, _stamp=stamp):   # noqa: ANN001
            try:
                _fn()
            except BaseException:
                r = _t._rec[0]
                r.update(ok=False, fails=int(r.get("fails") or 0) + 1)
                _t._persist()
                _t._hold[0] = time.monotonic() + _RETRY_S  # last: it is the signal
                raise

    def fn(_fn=inner, _t=t):   # noqa: ANN001
        # The busy flag is cleared for EVERY kind of task. Clearing it only in
        # the at_hour wrapper left every interval task (reindex, and the whole
        # AIFORGE_COMPACT_AT_HOUR=off schedule) running exactly once per process.
        try:
            _fn()
        finally:
            _t._busy[0] = False

    from aiforge_core.runtime import background as _bg
    t._busy[0] = True
    if _bg.spawn(fn, name=f"periodic:{t.name}") is None:
        # spawn never RAISES — it returns None when the thread couldn't start.
        # Without this the wrapper (and its finally) never runs and the task
        # stays "in flight" for the life of the process.
        t._busy[0] = False
        log.warning("periodic %s: could not launch — will retry next slot", t.name)
        if t.at_hour is not None:
            r = t._rec[0]
            r.update(ok=False, fails=int(r.get("fails") or 0) + 1)
            t._persist()
            t._hold[0] = time.monotonic() + _RETRY_S
        return
    log.info("periodic fired: %s", t.name)


def _due(t: _Task, now_mono: float, now_dt: datetime) -> float:
    """``_next_after`` that cannot kill the loop. One task's unreadable state
    must not stop reindex, graph maintenance and compaction for the life of the
    process."""
    try:
        return t._next_after(now_mono, now_dt)
    except Exception as exc:  # noqa: BLE001
        log.warning("periodic task %s scheduling failed: %s", t.name, exc)
        return 3600.0


def start() -> None:
    """Launch the single periodic loop (idempotent). No-op when disabled or no
    tasks are registered."""
    global _started
    if os.environ.get("AIFORGE_PERIODIC_DISABLE", "") in ("1", "true", "yes"):
        return
    with _lock:
        if _started or not _TASKS:
            return
        _started = True

    def _loop() -> None:
        while True:
            now_mono, now_dt = time.monotonic(), datetime.now()
            # sleep until the SOONEST due task, capped so at_hour tasks are
            # re-evaluated and new registrations get picked up.
            wait = min((_due(t, now_mono, now_dt) for t in _TASKS),
                       default=3600.0)
            # Floor keeps the loop cheap when idle (daily tasks → capped at 1h so
            # new registrations + at_hour re-eval happen); a task due sooner still
            # fires within ~1s.
            time.sleep(max(1.0, min(wait, 3600.0)))
            nm = time.monotonic()
            nd = datetime.now()
            for t in list(_TASKS):
                if _due(t, nm, nd) <= 1.0:
                    try:
                        _fire(t, nd)
                    except Exception as exc:  # noqa: BLE001 — never kill the loop
                        log.warning("periodic task %s fire failed: %s", t.name, exc)

    from aiforge_core.runtime import background as _bg
    _bg.spawn(_loop, name="periodic-loop")


__all__ = ["register", "start"]
