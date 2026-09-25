"""Scheduled-jobs store — embedded SQLite (SQLite-only build).

SQLite path ``$AIFORGE_JOBS_DB_PATH`` (default ``$AIFORGE_CONFIG_DIR/jobs.db``).

Timestamps are naive server-LOCAL ISO-8601 strings (second precision) — matching
the spec's cron semantics ("8am" = local 8am). They are stored as TEXT so
lexicographic comparison == chronological and the exact string round-trips.
Lexicographic comparison == chronological, with one
accepted edge: during a DST fall-back hour the wall clock (and thus the string)
repeats; the recompute-from-now in mark_fired prevents double-fires, so the
worst case is a fire shifted by up to an hour once a year.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from aiforge_core.config.paths import config_dir

_SELECT_FROM_JOBS_WHERE_ID = 'SELECT * FROM jobs WHERE id=?'

_LOCK = threading.Lock()

_UPDATABLE = {"name", "cron", "ticket_title", "ticket_body", "project",
              "enabled", "next_run_at", "last_error", "script_path",
              "expires_at", "session_id", "run_status", "run_token",
              "run_attempt", "run_pid"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_ts(ts: str) -> str:
    """Normalize any ISO-8601-ish timestamp to the store's canonical
    bare-seconds local format — the lexicographic due-query depends on
    every stored timestamp sharing this exact shape."""
    return datetime.fromisoformat(ts).replace(tzinfo=None) \
        .isoformat(timespec="seconds")


def _row(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return d


# ══════════════════════════════ SQLite backend ══════════════════════════════

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cron TEXT NOT NULL,
  ticket_title TEXT NOT NULL,
  ticket_body TEXT NOT NULL,
  project TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'ticket',
  script_path TEXT,
  expires_at TEXT
);
"""

# Columns added after the original schema shipped. Applied idempotently on
# every connect so an existing jobs.db (created before the ``script`` job kind)
# gains them without a manual migration. ``kind`` distinguishes ticket jobs
# (fire → create a ticket for the agent pipeline) from script jobs (fire → run
# a user-approved local script — deterministic ops, no LLM per tick).
# ``expires_at`` is when the job CLOSES ITSELF (see jobs/lifecycle.py). NULL
# means never — the explicit opt-out, not the default: everything created from
# chat gets an end so a monitoring loop cannot outlive the thing it watches.
_SQLITE_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'ticket'"),
    ("script_path", "TEXT"),
    ("expires_at", "TEXT"),
    # Chat session that created the job, so a finished run can report back
    # and a later message in that chat can steer the agent run.
    ("session_id", "INTEGER"),
    # A claimed slot whose worker has not finished. Startup resumes it once.
    ("run_status", "TEXT"),
    ("run_token", "TEXT"),
    ("run_attempt", "INTEGER NOT NULL DEFAULT 0"),
    # Script process group, so a restart can reattach instead of launching
    # a second copy while the first is still alive.
    ("run_pid", "INTEGER"),
)


# DB paths whose schema DDL+migrate has already run this process (keyed by path
# so per-test temp DBs each still get created).
_SQLITE_SCHEMA_DONE: set[str] = set()


def _migrate_sqlite(con) -> None:
    have = {r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, decl in _SQLITE_ADDED_COLUMNS:
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")


class _SqliteJobStore:
    name = "sqlite"

    def _db_path(self) -> str:
        raw = os.environ.get("AIFORGE_JOBS_DB_PATH")
        if raw:
            return os.path.expanduser(raw)
        cfg = str(config_dir())
        return os.path.join(os.path.expanduser(cfg), "jobs.db")

    @contextmanager
    def _conn(self):
        path = self._db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=30.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            # Run schema DDL + migrate ONCE per DB path, not on every connect
            # (executescript + ALTER-TABLE-ADD-COLUMN on each call was wasteful
            # and spammed caught "duplicate column" errors).
            if path not in _SQLITE_SCHEMA_DONE:
                con.executescript(_SQLITE_DDL)
                _migrate_sqlite(con)
                _SQLITE_SCHEMA_DONE.add(path)
            yield con
            con.commit()
        finally:
            con.close()

    def create(self, *, name, cron, ticket_title, ticket_body,
               project=None, next_run_at, kind="ticket",
               script_path=None, expires_at=None, session_id=None) -> dict:
        next_run_at = _norm_ts(next_run_at)
        expires_at = _norm_ts(expires_at) if expires_at else None
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "INSERT INTO jobs (name, cron, ticket_title, ticket_body, "
                "project, next_run_at, created_at, kind, script_path, "
                "expires_at, session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, cron, ticket_title, ticket_body, project,
                 next_run_at, now_iso(), kind, script_path, expires_at,
                 session_id))
            r = con.execute(_SELECT_FROM_JOBS_WHERE_ID,
                            (cur.lastrowid,)).fetchone()
            return _row(r)

    def get(self, job_id) -> "dict | None":
        with self._conn() as con:
            r = con.execute(_SELECT_FROM_JOBS_WHERE_ID, (job_id,)).fetchone()
            return _row(r) if r else None

    def list_jobs(self) -> list[dict]:
        with self._conn() as con:
            rs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [_row(r) for r in rs]

    def update(self, job_id, fields) -> "dict | None":
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with _LOCK, self._conn() as con:
            con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*vals, job_id))
            r = con.execute(_SELECT_FROM_JOBS_WHERE_ID, (job_id,)).fetchone()
            return _row(r) if r else None

    def update_if_token(self, job_id, token, fields) -> "dict | None":
        """Update only while ``token`` still owns this claimed slot.

        No token (tests / a direct dispatch) writes by id, same as
        ``_claim_still_held`` treating an empty token as still held."""
        if not token:
            return self.update(job_id, fields)
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with _LOCK, self._conn() as con:
            cur = con.execute(
                f"UPDATE jobs SET {sets} WHERE id=? AND run_token=?",
                (*vals, job_id, token))
            if (cur.rowcount or 0) == 0:
                return None
            r = con.execute(_SELECT_FROM_JOBS_WHERE_ID, (job_id,)).fetchone()
            return _row(r) if r else None

    def delete(self, job_id) -> bool:
        with _LOCK, self._conn() as con:
            cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return cur.rowcount > 0

    def due_jobs(self, now) -> list[dict]:
        with self._conn() as con:
            rs = con.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run_at<=? "
                "ORDER BY id", (now,)).fetchall()
            return [_row(r) for r in rs]

    def expired_jobs(self, now) -> list[dict]:
        """Jobs whose end time has passed — INCLUDING disabled ones, because a
        paused loop is still a loop nobody closed. Same lexicographic-compare
        trick as due_jobs; NULL expires_at never matches, which is what makes
        `until=forever` mean forever."""
        with self._conn() as con:
            rs = con.execute(
                "SELECT * FROM jobs WHERE expires_at IS NOT NULL "
                "AND expires_at<=? ORDER BY id", (now,)).fetchall()
            return [_row(r) for r in rs]

    def mark_fired(self, job_id, *, last_run_at, next_run_at,
                   last_error=None) -> None:
        last_run_at = _norm_ts(last_run_at)
        next_run_at = _norm_ts(next_run_at)
        with _LOCK, self._conn() as con:
            con.execute(
                "UPDATE jobs SET last_run_at=?, next_run_at=?, last_error=? "
                "WHERE id=?", (last_run_at, next_run_at, last_error, job_id))

    def claim(self, job_id, *, expected_next_run_at, last_run_at, next_run_at,
              last_error=None) -> bool:
        """Atomic compare-and-swap advance: only succeeds if the row is STILL at
        ``expected_next_run_at`` (the slot we intend to fire). Two racers
        (run-now + the tick) both read the same due job, but only the FIRST
        claim's WHERE matches — the second sees the already-advanced slot and
        gets rowcount 0, so it must not fire. Prevents the double-fire the old
        non-atomic mark_fired allowed."""
        token = uuid.uuid4().hex
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "UPDATE jobs SET last_run_at=?, next_run_at=?, last_error=?, "
                "run_status='running', run_token=?, run_attempt=1 "
                "WHERE id=? AND next_run_at=?",
                (_norm_ts(last_run_at), _norm_ts(next_run_at), last_error,
                 token, job_id, _norm_ts(expected_next_run_at)))
            return (cur.rowcount or 0) > 0

    def inflight_jobs(self) -> list[dict]:
        """Jobs whose claimed run never finished (process died mid-flight)."""
        with self._conn() as con:
            rs = con.execute(
                "SELECT * FROM jobs WHERE run_status='running' ORDER BY id"
            ).fetchall()
            return [_row(r) for r in rs]

    def clear_run(self, job_id, token) -> bool:
        """Drop the in-flight mark only if this worker still owns it."""
        if not token:
            return False
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "UPDATE jobs SET run_status=NULL, run_token=NULL, "
                "run_attempt=0 WHERE id=? AND run_token=?",
                (job_id, token))
            return (cur.rowcount or 0) > 0

    def bump_inflight(self, job_id, expected_attempt: int) -> bool:
        """One process wins the startup retry. Returns True only for the winner."""
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "UPDATE jobs SET run_attempt=? WHERE id=? "
                "AND run_status='running' AND run_attempt=?",
                (int(expected_attempt) + 1, job_id, int(expected_attempt)))
            return (cur.rowcount or 0) > 0

    def release_retried(self, job_id, last_error: str) -> bool:
        """Clear an in-flight row that was already retried. One winner only."""
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "UPDATE jobs SET run_status=NULL, run_token=NULL, "
                "run_attempt=0, last_error=? WHERE id=? "
                "AND run_status='running' AND run_attempt>=2",
                (last_error, job_id))
            return (cur.rowcount or 0) > 0


# ═══════════════════════════ backend selection ═══════════════════════════════

_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def _backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = _SqliteJobStore()
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None


# ═══════════════════════════ public function API ═════════════════════════════

def create(*, name: str, cron: str, ticket_title: str, ticket_body: str,
           project: str | None = None, next_run_at: str,
           kind: str = "ticket", script_path: str | None = None,
           expires_at: str | None = None, session_id: int | None = None) -> dict:
    return _backend().create(
        name=name, cron=cron, ticket_title=ticket_title,
        ticket_body=ticket_body, project=project, next_run_at=next_run_at,
        kind=kind, script_path=script_path, expires_at=expires_at,
        session_id=session_id)


def get(job_id: int) -> "dict | None":
    return _backend().get(job_id)


def list_jobs() -> list[dict]:
    return _backend().list_jobs()


def update(job_id: int, **fields) -> "dict | None":
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"unknown job fields: {sorted(bad)}")
    if not fields:
        return get(job_id)
    if fields.get("next_run_at") is not None:
        fields["next_run_at"] = _norm_ts(fields["next_run_at"])
    return _backend().update(job_id, fields)


def update_if_token(job_id: int, token: str | None, **fields) -> "dict | None":
    """Write fields only if this run_token still owns the row.

    Returns the row on success, None when a later claim already owns it.
    No token falls back to ``update`` (direct dispatch / tests)."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"unknown job fields: {sorted(bad)}")
    if not fields:
        return get(job_id)
    if fields.get("next_run_at") is not None:
        fields["next_run_at"] = _norm_ts(fields["next_run_at"])
    return _backend().update_if_token(job_id, token, fields)


def delete(job_id: int) -> bool:
    return _backend().delete(job_id)


def due_jobs(now: str) -> list[dict]:
    """Enabled jobs whose next_run_at has passed. A job missed while the
    service was down is naturally 'due' at startup — catch-up-once falls
    out of this query plus mark_fired recomputing from *now*."""
    return _backend().due_jobs(now)


def expired_jobs(now: str) -> list[dict]:
    """Jobs past their end time, enabled or not. The scheduler sweeps these
    through jobs.lifecycle.close_job — learning kept, script kept, row gone."""
    return _backend().expired_jobs(now)


def mark_fired(job_id: int, *, last_run_at: str, next_run_at: str,
               last_error: str | None = None) -> None:
    return _backend().mark_fired(
        job_id, last_run_at=last_run_at, next_run_at=next_run_at,
        last_error=last_error)


def claim(job_id: int, *, expected_next_run_at: str, last_run_at: str,
          next_run_at: str, last_error: str | None = None) -> bool:
    """Atomically claim + advance a job's due slot. Returns True iff THIS caller
    won the slot (the row was still at ``expected_next_run_at``). Only the winner
    should fire — prevents run-now + tick (or multi-replica) double-fires.

    The winner is also marked in-flight (``run_status='running'``) so a process
    restart can retry that claimed run once instead of dropping it."""
    return _backend().claim(
        job_id, expected_next_run_at=expected_next_run_at,
        last_run_at=last_run_at, next_run_at=next_run_at, last_error=last_error)


def inflight_jobs() -> list[dict]:
    return _backend().inflight_jobs()


def clear_run(job_id: int, token: str | None) -> bool:
    return _backend().clear_run(job_id, token)


def bump_inflight(job_id: int, expected_attempt: int) -> bool:
    return _backend().bump_inflight(job_id, expected_attempt)


def release_retried(job_id: int, last_error: str) -> bool:
    return _backend().release_retried(job_id, last_error)
