"""The rate ceiling's window, shared by every AIForge process on this machine.

WHY THIS EXISTS. ``rate_limiter``'s window lived in a module global, so it was
per-PROCESS — and ``run.sh`` starts more than one process that talks to the
model:

  * ``uvicorn`` — chat, the routers, jobs, the memory fold      (run.sh)
  * ``aiforge_core.runtime.adk_runner`` — the whole team pipeline, own PID
  * ``aiforge_core.deploy.converge`` — runs the startup fold at boot, own PID

Each one independently allowed the operator's ``llm_max_rpm``. Set 15 against a
gateway that permits 20/min and two live senders put 30 on the wire, so the
number in Settings meant nothing on the wire and the rejections kept coming —
with the setting correctly applied in every process. One number in Settings has
to mean one number at the endpoint.

WHY SQLITE. The processes already share ``AIFORGE_CONFIG_DIR`` for settings, so
they can share a file there. A counter file guarded by ``fcntl`` would not port
to Windows, and a lock-file protocol invites stale locks after a SIGKILL —
which is exactly how ``run.sh`` reaps its children. SQLite gives cross-process
atomicity (``BEGIN IMMEDIATE``), releases its lock when the process dies
however it dies, and is in the stdlib. A few milliseconds per acquire against
an LLM call measured in seconds is not a cost worth optimising.

WALL CLOCK, DELIBERATELY — AND THIS REVERSES THE IN-PROCESS CHOICE.
``rate_limiter`` uses ``time.monotonic()`` precisely because a wall-clock step
would leave stamps that never age out. That reasoning does not survive contact
with a second process: monotonic clocks have an arbitrary per-process origin,
so two processes' monotonic readings are not comparable and a shared window
built on them is nonsense. Wall time is the only clock they agree on. The
danger monotonic was protecting against is handled head-on instead — see
the pruning rule in ``take``: a stamp in the future, or older than the window,
is deleted rather than trusted, so a clock step costs at most one window of
over-sending instead of disabling the ceiling until someone notices. Holds get
the same treatment at both ends — see ``_MAX_HOLD_S``.

A THROTTLE, NOT A SCHEDULER. Whichever process wins the write lock can drain
the whole window in microseconds, so slots are not shared fairly between
processes — a busy pipeline can starve chat for the rest of the minute. The
property this guarantees is the one the provider cares about: never more than
`llm_max_rpm` in any 60 seconds across the machine. Fair division would need a
per-process reservation, which is a different (and much easier to get wrong)
design.

NEVER FAILS A CALL. Every entry point swallows its own errors and reports "no
opinion" (``None``), and the caller falls back to the in-process window. A
locked database, a read-only config dir or a corrupt file must throttle
slightly worse, never break the product.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

_BEGIN_IMMEDIATE = 'BEGIN IMMEDIATE'

log = logging.getLogger("aiforge.rate_limiter")

_LOCAL = threading.local()
# One connection per thread per process. sqlite3 objects are not safe to share
# across threads, and the producer pool has eight of them.

# How long to wait for ANOTHER PROCESS's write lock. Deliberately SHORT.
# The window is held for microseconds, so reaching this means locking does not
# work on this filesystem at all — Docker Desktop's gRPC-FUSE bind mount, WSL
# with $HOME on /mnt/c DrvFs, an NFS/SMB config dir. At the 5s this started
# with, such a box paid five seconds on EVERY model call and still got a
# per-process ceiling: strictly worse than not having the feature.
_LOCK_TIMEOUT_S = 0.5
# Attempts to get a brand-new database into shape. See _init.
_INIT_TRIES = 8
# After this many consecutive failures, stop trying for _COOLDOWN_S. Without
# it, an unusable store is re-probed on every single call forever.
_FAIL_LIMIT = 3
_COOLDOWN_S = 60.0
_DEGRADED_LOCK = threading.Lock()
_fails = 0            # CONSECUTIVE failures — drives the cooldown
_total_fails = 0      # lifetime failures — drives the one-shot warning
_cold_until = 0.0
_warned = False


def _degrade(exc: "Exception | None") -> None:
    """Record a failure, and say so ONCE.

    A silent fallback here IS the bug this module exists to fix — the ceiling
    quietly goes back to per-process and the operator sees the same
    rate-limit rejections with the same setting. One warning, then quiet.
    """
    global _fails, _cold_until, _warned, _total_fails
    say = False
    with _DEGRADED_LOCK:
        _fails += 1
        # The WARNING is driven off the lifetime count, not the consecutive
        # one. count() calls _healthy() and the toolbar polls /api/llm/usage
        # every 3 seconds, so in the read-succeeds/write-fails split — the
        # exact state `writable()` was added to detect — a laundering read
        # reset the run between every failing write and the operator was told
        # nothing across 480 model calls on a private window. The COOLDOWN
        # still keys off consecutive failures, because that is a "stop
        # retrying for a bit" decision and one success genuinely disproves it.
        _total_fails += 1
        if not _warned and _total_fails >= _FAIL_LIMIT:
            _warned = say = True
        if _fails < _FAIL_LIMIT:
            if not say:
                return
        else:
            _cold_until = time.time() + _COOLDOWN_S
            _fails = 0
    if not say:
        return
    log.warning(
        "llm.shared_window_unavailable: %s at %s — the calls-per-minute "
        "ceiling has fallen back to PER-PROCESS, so each AIForge process gets "
        "the full llm_max_rpm and the total on the wire is a multiple of it. "
        "Usually a config dir where SQLite locking does not work (a network "
        "mount, a Docker bind mount, WSL on /mnt/c) or one owned by another "
        "user. Set AIFORGE_CONFIG_DIR to local disk, or divide llm_max_rpm by "
        "the number of processes.", type(exc).__name__ if exc else "unavailable",
        path())


def _healthy() -> None:
    """One success clears the failure run.

    Without this call — and it WAS defined and never called — `_fails` counts
    every failure for the life of the process rather than consecutive ones, so
    three stumbles hours apart trip a 60-second cooldown in which the process
    runs on its private window with the whole `llm_max_rpm`, and shared holds
    are invisible to it. Measured at 331 sends against a ceiling of 50: 6.6x,
    worse than the 3x bug this module exists to fix.
    """
    global _fails
    if _fails:
        with _DEGRADED_LOCK:
            _fails = 0


def _cold() -> bool:
    return time.time() < _cold_until


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
    _LOCAL.db = None
    _LOCAL.path = None


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
    if not (c > 0):          # also catches nan
        c = _DEFAULT_CAP_S
    return min(c * 1.5 + 5.0, _MAX_HOLD_S)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sends_ts ON sends (ts);
CREATE TABLE IF NOT EXISTS holds (k TEXT PRIMARY KEY, until REAL NOT NULL);
"""


def path() -> str:
    """Where the shared window lives. Same directory the settings store uses,
    so anything that can read the ceiling can also count against it."""
    root = os.environ.get("AIFORGE_CONFIG_DIR") or os.path.expanduser("~/.aiforge")
    return os.path.join(root, "llm_rate.db")


def enabled() -> bool:
    """``AIFORGE_LLM_SHARED_WINDOW=0`` falls back to the per-process window.

    An escape hatch, not a feature: if the shared file ever misbehaves on a
    machine, an operator needs a way back to the previous behaviour that does
    not involve editing code.
    """
    return (os.environ.get("AIFORGE_LLM_SHARED_WINDOW", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _init(db: "sqlite3.Connection") -> None:
    """Put a fresh database into the shape we need, tolerating a cold-start
    stampede.

    SQLite takes an EXCLUSIVE lock to change the journal mode and — unlike an
    ordinary write — **does not run the busy handler for it**, so `timeout`
    buys nothing here and concurrent first-opens simply fail. That is exactly
    the `run.sh` cold start: uvicorn, the pipeline runner and the boot-time
    fold all reach a config dir with no `llm_rate.db` at the same instant, and
    the fold is a burst of model calls at precisely that moment. Measured: ~19%
    of opens failed, and each failure handed that process a full private
    allowance. Retry briefly instead.

    WAL IS OPTIONAL. It is unavailable on the filesystems this module's own
    timeout comment names (gRPC-FUSE bind mounts, WSL on /mnt/c, NFS/SMB),
    because it needs mmap-able shared memory. Rollback-journal mode is slower
    and serialises readers against the writer, but it is CORRECT — and correct
    and slow beats a permanently unavailable ceiling.
    """
    for attempt in range(_INIT_TRIES):
        try:
            db.execute("PRAGMA journal_mode=WAL")
            break
        except Exception:  # noqa: BLE001
            if attempt == _INIT_TRIES - 1:
                break          # keep the connection; the default journal works
            time.sleep(0.02 * (attempt + 1))
    db.execute("PRAGMA synchronous=NORMAL")
    for attempt in range(_INIT_TRIES):
        try:
            db.executescript(_SCHEMA)
            return
        except Exception:  # noqa: BLE001
            if attempt == _INIT_TRIES - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _conn() -> "sqlite3.Connection | None":
    """This thread's connection, or None if the store cannot be opened."""
    if _cold():
        return None
    db = getattr(_LOCAL, "db", None)
    want = path()
    if db is not None and getattr(_LOCAL, "path", None) == want:
        return db
    if db is not None:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        _LOCAL.db = None
    try:
        os.makedirs(os.path.dirname(want), exist_ok=True)
        db = sqlite3.connect(want, timeout=_LOCK_TIMEOUT_S,
                             isolation_level=None)
        _init(db)
    except Exception as exc:  # noqa: BLE001 — a limiter must never break a call
        _LOCAL.db = None
        _LOCAL.path = None
        _degrade(exc)
        return None
    _LOCAL.db = db
    _LOCAL.path = want
    return db


def count(now: float | None = None) -> "int | None":
    """Sends in the last 60s across every process, or None if unavailable."""
    db = _conn()
    if db is None:
        return None
    now = time.time() if now is None else now
    try:
        cur = db.execute("SELECT COUNT(*) FROM sends WHERE ts > ? AND ts <= ?",
                         (now - 60.0, now + 1.0))
        n = int(cur.fetchone()[0])
        _healthy()
        return n
    except Exception as exc:  # noqa: BLE001
        if not _busy(exc):
            _degrade(exc)
        return None


def take(limit: int, now: float | None = None) -> "tuple[bool, float] | None":
    """Try to claim one send. Returns ``(claimed, seconds_to_wait)``.

    ``(True, 0.0)`` — counted, go. ``(False, n)`` — the window is full and the
    oldest send ages out in ``n`` seconds. ``None`` — the shared store had no
    opinion; use the in-process window.

    The delete, the count and the insert are ONE transaction on purpose. Split
    across statements, two processes both read "14 of 15" and both send.
    """
    db = _conn()
    if db is None:
        return None
    _live = now is None
    now = time.time() if now is None else now
    try:
        try:
            db.execute(_BEGIN_IMMEDIATE)
            # RE-READ THE CLOCK INSIDE THE LOCK. Read outside it, `now` could
            # be up to _LOCK_TIMEOUT_S stale — and across a clock step that
            # made the prune below delete the whole window that the callers on
            # the other side of the step had just filled. Every straggler then
            # cost another full window: measured 2080 grants against a ceiling
            # of 50 at 6 processes x 8 threads, where the docstring promises
            # "at most one window". Re-reading makes the staleness ~0.
            # (BEGIN IMMEDIATE moved INSIDE this try as well: an interrupt
            # delivered between it returning and the try being entered left the
            # transaction open on a cached connection — M1, in a one-bytecode
            # window.)
            if _live:
                now = time.time()
            # Prune both directions: aged out, and IMPOSSIBLE. A stamp in the
            # future is the signature of a backwards clock step (written
            # before it, read after); trusting one keeps the window full for
            # the length of the step, which is how a wall-clock window becomes
            # a silent kill switch. Deleting it over-sends by at most one
            # window — the recoverable direction.
            db.execute("DELETE FROM sends WHERE ts <= ? OR ts > ?",
                       (now - 60.0, now + 1.0))
            n = int(db.execute("SELECT COUNT(*) FROM sends").fetchone()[0])
            if n < limit:
                db.execute("INSERT INTO sends (ts) VALUES (?)", (now,))
                db.execute("COMMIT")
                _healthy()
                return True, 0.0
            oldest = db.execute("SELECT MIN(ts) FROM sends").fetchone()[0]
            db.execute("COMMIT")
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt (Ctrl-C into
            # run.sh's process group), SystemExit or CancelledError arriving
            # between BEGIN IMMEDIATE and COMMIT left this thread's cached
            # connection inside an open write transaction forever — and since
            # nothing ever discarded it, every OTHER process then paid the full
            # lock timeout on every model call before falling back too. With no
            # warning: _degrade is only reached from _conn, which had succeeded.
            _rollback(db)
            raise
    except Exception as exc:  # noqa: BLE001
        if _busy(exc):
            # Busy, not broken. A short retry keeps this caller on the SHARED
            # window instead of quietly promoting it to a private one.
            return False, 0.05
        _degrade(exc)
        return None
    _healthy()
    if oldest is None:                      # raced empty; caller retries
        return False, 0.0
    return False, max(0.0, (float(oldest) + 60.0) - now)


def force(limit: int, now: float | None = None) -> bool:
    """Count a send that is going out regardless (the overrun path).

    It left the box, so it belongs in the window whatever the count says —
    otherwise a queue of overrunning callers each decide they are the one
    exception and the shared count stops describing the traffic.

    CLAMPED to ``limit``, like the in-process window it replaces: an unclamped
    count blocks the next well-behaved caller for a full 60s instead of
    60/rpm, and ships a `limit_used` above `limit_rpm` for the toolbar to
    render as nonsense.
    """
    db = _conn()
    if db is None:
        return False
    _live = now is None
    now = time.time() if now is None else now
    try:
        try:
            db.execute(_BEGIN_IMMEDIATE)
            if _live:
                now = time.time()       # see take(): a stale clock prunes wrongly
            # Prune first, both directions. Without it the trim below ranks
            # FUTURE-stamped rows (a backwards clock step) as "newest", so they
            # survived and the row just inserted was evicted instead: force()
            # returned True having deleted its own send.
            db.execute("DELETE FROM sends WHERE ts <= ? OR ts > ?",
                       (now - 60.0, now + 1.0))
            db.execute("INSERT INTO sends (ts) VALUES (?)", (now,))
            db.execute(
                "DELETE FROM sends WHERE rowid IN ("
                "  SELECT rowid FROM sends ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (max(1, int(limit)),))
            db.execute("COMMIT")
            _healthy()
        except BaseException:
            _rollback(db)
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        # A busy store means this send was NOT counted, and force()'s whole
        # contract is that a send which left the box is counted whatever the
        # window says. Report the miss so the caller can retry rather than
        # silently losing it.
        if not _busy(exc):
            _degrade(exc)
        return False


def set_hold(key: str, until_ts: float, cap: "float | None" = None) -> None:
    """Record a server-imposed hold so EVERY process observes it.

    This is the half that matters most across processes: only one of them gets
    the 429, and without a shared hold the others keep sending into a wall the
    server has already named.
    """
    db = _conn()
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
            _degrade(exc)


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
    db = _conn()
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
            _degrade(exc)
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
    db = _conn()
    if db is None:
        return False
    try:
        try:
            db.execute(_BEGIN_IMMEDIATE)
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


def exists() -> bool:
    """Is there a store on disk at all? Lets a caller skip opening one just to
    clear it — the test suite resets twice per test, thousands of times."""
    try:
        return os.path.exists(path())
    except Exception:  # noqa: BLE001
        return False


def reset() -> None:
    """Test helper — drop every shared send and hold, and disarm degradation.

    The cooldown and the one-shot warning are process globals. Leaving them
    armed across a reset means one test that simulated a broken store silently
    disables the shared window for every test after it — which is both a
    surprise here and, in production, the difference between "the ceiling is
    machine-wide" and "it quietly is not".
    """
    global _fails, _total_fails, _cold_until, _warned
    with _DEGRADED_LOCK:
        _fails = _total_fails = 0
        _cold_until = 0.0
        _warned = False
    if not exists():
        return
    db = _conn()
    if db is None:
        return
    try:
        db.execute("DELETE FROM sends")
        db.execute("DELETE FROM holds")
    except Exception:  # noqa: BLE001
        pass


def close() -> None:
    """Drop this thread's connection (tests that move AIFORGE_CONFIG_DIR)."""
    db = getattr(_LOCAL, "db", None)
    if db is not None:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    _LOCAL.db = None
    _LOCAL.path = None


__all__ = ["path", "enabled", "exists", "writable", "count", "take", "force",
           "set_hold", "hold_left", "reset", "close"]
