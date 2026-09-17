"""The shared window's store helpers and holds: rolling back a failed write,
busy retries, per-category caps, and the hold a 429 sets."""
from __future__ import annotations

import math
import sqlite3
import time


def _pkg():
    """The package, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.llm._shared_window as package
    return package


def _rollback(db) -> None:
    """Undo a half-finished transaction and drop the connection.

    Dropping it is the load-bearing half: a connection that failed mid-write
    may still hold the lock, and it is cached per-thread, so keeping it wedges
    this thread and blocks every other process indefinitely.
    """
    try:
        db.execute("ROLLBACK")
    except Exception:  # noqa: BLE001
        pass
    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass
    _pkg()._LOCAL.db = None
    _pkg()._LOCAL.path = None


def _busy(exc: Exception) -> bool:
    """Is this "another process holds the lock" rather than "broken"?

    THE DISTINCTION IS THE WHOLE FEATURE. A busy store is a WORKING store —
    someone else is counting in it right now. Reporting that as "no opinion"
    sends the caller to its own in-process window, which hands out a slot the
    shared window would have refused: measured at 12 grants for a ceiling of
    10, i.e. the exact over-sending this module exists to stop, appearing only
    under the contention it exists to handle.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    m = str(exc).lower()
    return "locked" in m or "busy" in m

# Absolute backstop, used only when a caller does not say what its own cap is.
# The REAL bound is the caller's: see the `cap` argument on set_hold/hold_left.
# A fixed hour here was the bug — the largest hold anything legitimately writes
# is llm_rate_limit_cap_s (default 60), so an hour-wide clamp honoured a
# backwards clock step of up to 59 minutes verbatim, and held_for() takes
# max(in-process, shared), so the poisoned wall-clock value overrode the
# monotonic one that was immune. The ceiling was then off for the length of the
# step while logging that it was working: precisely the silent kill switch the
# in-process design chose monotonic to avoid.
_MAX_HOLD_S = 3600.0


# What a caller gets when it does not say. Deliberately NOT _MAX_HOLD_S: an
# hour-wide default is precisely the C3 bug, and these functions are public.
_DEFAULT_CAP_S = 60.0


def _cap(cap: "float | None") -> float:
    """The widest a hold may be, plus an allowance for the round trip that
    produced it.

    PROPORTIONAL, not a flat minute: a flat +60 swamped small caps, so
    llm_rate_limit_cap_s=1 still honoured a 61-second hold. And nan/0/negative
    fall back to the default rather than to the hour-wide backstop.
    """
    try:
        c = float(cap) if cap is not None else 0.0
    except (TypeError, ValueError):
        c = 0.0
    # isnan explicitly: `c <= 0` is FALSE for nan, so the obvious spelling
    # would let a nan cap through and every later comparison with it would be
    # false too. The old `not (c > 0)` caught it by accident of IEEE ordering,
    # which is a fact about floats, not something the next reader should have
    # to reconstruct.
    if math.isnan(c) or c <= 0:
        c = _DEFAULT_CAP_S
    return min(c * 1.5 + 5.0, _MAX_HOLD_S)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (ts REAL NOT NULL, cat TEXT NOT NULL DEFAULT 'chat');
CREATE INDEX IF NOT EXISTS sends_ts ON sends (ts);
CREATE TABLE IF NOT EXISTS holds (k TEXT PRIMARY KEY, until REAL NOT NULL);
"""


def _migrate_cat(db: "sqlite3.Connection") -> None:
    """Add the ``cat`` column to a ``sends`` table created before per-category
    ceilings existed. Idempotent: a fresh table already has it (see _SCHEMA), so
    this only fires on an upgrade over an old ``llm_rate.db``. A pre-existing row
    predates categories, so it counts as 'chat' (the busy interactive bucket) —
    the conservative default that never under-counts the compaction bucket.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(sends)").fetchall()}
    if "cat" not in cols:
        db.execute("ALTER TABLE sends ADD COLUMN cat TEXT NOT NULL DEFAULT 'chat'")


def set_hold(key: str, until_ts: float, cap: "float | None" = None) -> None:
    """Record a server-imposed hold so EVERY process observes it.

    This is the half that matters most across processes: only one of them gets
    the 429, and without a shared hold the others keep sending into a wall the
    server has already named.
    """
    db = _pkg()._conn()
    if db is None:
        return
    # CLAMPED ON WRITE. `MAX(until, excluded.until)` means one poisoned row
    # wins forever: a forward clock step, a laptop resume or a container with a
    # bad clock wrote `now + 90000`, and from then on every real 429 for that
    # provider was silently ignored — on disk, with no log line and no cure
    # short of deleting the file.
    now = time.time()
    lim = _cap(cap)
    until = min(float(until_ts), now + lim)
    try:
        db.execute(
            "INSERT INTO holds (k, until) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET until=MAX("
            "  MIN(holds.until, ?), excluded.until)",
            (key, until, now + lim))
    except Exception as exc:  # noqa: BLE001
        # Busy is not broken. set_hold runs from note_rate_limited — during a
        # 429 storm, when every process on the box is writing a hold at once —
        # so counting contention as failure armed the cooldown at the one
        # moment the shared ceiling matters most.
        if not _busy(exc):
            _pkg()._degrade(exc)


def _drop_poisoned_hold(db, keys, marks, now, cap, left):
    """A hold beyond the cap was written before a clock moved: delete poisoned rows, re-query the real MAX, and return the seconds left (busy contention preserves the current hold, not a false clear)."""
    try:
        db.execute("DELETE FROM holds WHERE until > ?", (now + _cap(cap),))
        # RE-QUERY. MAX() across the keys means a poisoned catch-all row
        # can outrank a perfectly good provider hold, so returning 0 here
        # sent one call into a wall the server had already named.
        row = db.execute(
            f"SELECT MAX(until) FROM holds WHERE k IN ({marks})",  # noqa: S608
            keys).fetchone()
    except Exception as exc:  # noqa: BLE001
        # Busy is not "no hold". Several processes write holds at once
        # during a 429 storm, so treating contention here as "clear to
        # send" discards a legitimate hold at the worst moment — the same
        # class of bug as the poisoned row this branch exists to clean up.
        if _busy(exc):
            return max(0.0, min(left, _cap(cap)))
        return 0.0
    if not row or row[0] is None:
        return 0.0
    return max(0.0, min(float(row[0]) - now, _cap(cap)))


def hold_left(keys: "tuple[str, ...]", now: float | None = None,
              cap: "float | None" = None) -> "float | None":
    """Seconds left on the longest hold matching any of ``keys``."""
    db = _pkg()._conn()
    if db is None:
        return None
    now = time.time() if now is None else now
    try:
        marks = ",".join("?" * len(keys))
        row = db.execute(
            f"SELECT MAX(until) FROM holds WHERE k IN ({marks})",  # noqa: S608
            keys).fetchone()
    except Exception as exc:  # noqa: BLE001
        if not _busy(exc):
            _pkg()._degrade(exc)
        return None
    if not row or row[0] is None:
        return 0.0
    left = float(row[0]) - now
    if left > _cap(cap):
        # Written before a clock moved. Reading it as a hold would park every
        # caller for the length of the step; leaving it would keep swallowing
        # real holds (see set_hold). Drop it and carry on unheld.
        return _drop_poisoned_hold(db, keys, marks, now, cap, left)
    return max(0.0, left)


def writable() -> bool:
    """Can we actually COUNT A SEND right now?

    A read is the wrong probe: WAL readers never block on a writer, so
    ``count()`` happily returns a number while every ``take()`` fails — which
    is exactly the state an operator is trying to diagnose. Probe the write
    path, then undo it.
    """
    db = _pkg()._conn()
    if db is None:
        return False
    try:
        try:
            db.execute(_pkg()._BEGIN_IMMEDIATE)
            db.execute("DELETE FROM sends WHERE ts < 0")
            db.execute("COMMIT")
        except BaseException:
            _rollback(db)
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        # Busy means the store is ALIVE and someone else is writing to it —
        # reporting that as "the ceiling is per-process now" is the opposite of
        # the truth, and it happened on 4 of 28 probes under contention.
        return _busy(exc)
