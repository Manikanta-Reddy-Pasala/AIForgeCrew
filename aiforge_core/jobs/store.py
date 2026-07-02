"""Scheduled-jobs store — backend-neutral (SQLite or Postgres).

Same public function API + return shapes regardless of backend; the backend is
chosen ONCE per process by ``AIFORGE_PG_URL`` (the data-driven switch), exactly
like the tickets + chat stores. SQLite (path ``$AIFORGE_JOBS_DB_PATH``, default
``$AIFORGE_CONFIG_DIR/jobs.db``) is the ``--lite`` default; Postgres takes over
in the docker/hybrid stack so scheduled jobs live beside tickets, not in a
stray ``.db`` file.

Timestamps are naive server-LOCAL ISO-8601 strings (second precision) — matching
the spec's cron semantics ("8am" = local 8am). They are stored as TEXT in BOTH
backends (even Postgres) so lexicographic comparison == chronological and the
exact string round-trips. Lexicographic comparison == chronological, with one
accepted edge: during a DST fall-back hour the wall clock (and thus the string)
repeats; the recompute-from-now in mark_fired prevents double-fires, so the
worst case is a fire shifted by up to an hour once a year.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from aiforge_core.config import env as _env

_LOCK = threading.Lock()

_UPDATABLE = {"name", "cron", "ticket_title", "ticket_body", "project",
              "enabled", "next_run_at", "last_error"}


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
  script_path TEXT
);
"""

# Columns added after the original schema shipped. Applied idempotently on
# every connect so an existing jobs.db (created before the ``script`` job kind)
# gains them without a manual migration. ``kind`` distinguishes ticket jobs
# (fire → create a ticket for the agent pipeline) from script jobs (fire → run
# a user-approved local script — deterministic ops, no LLM per tick).
_SQLITE_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'ticket'"),
    ("script_path", "TEXT"),
)


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
        cfg = os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")
        return os.path.join(os.path.expanduser(cfg), "jobs.db")

    @contextmanager
    def _conn(self):
        path = self._db_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=30.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SQLITE_DDL)
            _migrate_sqlite(con)
            yield con
            con.commit()
        finally:
            con.close()

    def create(self, *, name, cron, ticket_title, ticket_body,
               project=None, next_run_at, kind="ticket",
               script_path=None) -> dict:
        next_run_at = _norm_ts(next_run_at)
        with _LOCK, self._conn() as con:
            cur = con.execute(
                "INSERT INTO jobs (name, cron, ticket_title, ticket_body, "
                "project, next_run_at, created_at, kind, script_path) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, cron, ticket_title, ticket_body, project,
                 next_run_at, now_iso(), kind, script_path))
            r = con.execute("SELECT * FROM jobs WHERE id=?",
                            (cur.lastrowid,)).fetchone()
            return _row(r)

    def get(self, job_id) -> "dict | None":
        with self._conn() as con:
            r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
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
            r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
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

    def mark_fired(self, job_id, *, last_run_at, next_run_at,
                   last_error=None) -> None:
        last_run_at = _norm_ts(last_run_at)
        next_run_at = _norm_ts(next_run_at)
        with _LOCK, self._conn() as con:
            con.execute(
                "UPDATE jobs SET last_run_at=?, next_run_at=?, last_error=? "
                "WHERE id=?", (last_run_at, next_run_at, last_error, job_id))


# ══════════════════════════════ Postgres backend ═════════════════════════════

_PG_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  cron text NOT NULL,
  ticket_title text NOT NULL,
  ticket_body text NOT NULL,
  project text,
  enabled boolean NOT NULL DEFAULT TRUE,
  last_run_at text,
  next_run_at text NOT NULL,
  last_error text,
  created_at text NOT NULL,
  kind text NOT NULL DEFAULT 'ticket',
  script_path text
);
"""

# Idempotent column adds for a jobs table created before the ``script`` kind.
_PG_MIGRATE = (
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS kind text NOT NULL "
    "DEFAULT 'ticket';"
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS script_path text;"
)


class _PgJobStore:
    name = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    @contextmanager
    def _conn(self):
        import psycopg
        c = psycopg.connect(self.dsn, autocommit=False, connect_timeout=5,
                            options="-c statement_timeout=15000")
        try:
            self._ensure_schema(c)
            yield c
        finally:
            c.close()

    def _ensure_schema(self, c) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            try:
                with c.cursor() as cur:
                    cur.execute(_PG_DDL)
                    cur.execute(_PG_MIGRATE)
                c.commit()
            except Exception:
                c.rollback()
            self._schema_ready = True

    def _cur(self, c):
        from psycopg.rows import dict_row
        return c.cursor(row_factory=dict_row)

    def create(self, *, name, cron, ticket_title, ticket_body,
               project=None, next_run_at, kind="ticket",
               script_path=None) -> dict:
        next_run_at = _norm_ts(next_run_at)
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(
                "INSERT INTO jobs (name, cron, ticket_title, ticket_body, "
                "project, next_run_at, created_at, kind, script_path) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (name, cron, ticket_title, ticket_body, project,
                 next_run_at, now_iso(), kind, script_path))
            r = cur.fetchone()
            c.commit()
        return _row(r)

    def get(self, job_id) -> "dict | None":
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
            r = cur.fetchone()
        return _row(r) if r else None

    def list_jobs(self) -> list[dict]:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM jobs ORDER BY id")
            rs = cur.fetchall()
        return [_row(r) for r in rs]

    def update(self, job_id, fields) -> "dict | None":
        # Postgres 'enabled' is a real boolean — keep Python bools as-is
        # (unlike SQLite, which needs 0/1 ints).
        sets = ", ".join(f"{k}=%s" for k in fields)
        vals = list(fields.values())
        with self._conn() as c, self._cur(c) as cur:
            cur.execute(f"UPDATE jobs SET {sets} WHERE id=%s RETURNING *",
                        (*vals, job_id))
            r = cur.fetchone()
            c.commit()
        return _row(r) if r else None

    def delete(self, job_id) -> bool:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
            deleted = (cur.rowcount or 0) > 0
            c.commit()
        return deleted

    def due_jobs(self, now) -> list[dict]:
        with self._conn() as c, self._cur(c) as cur:
            cur.execute("SELECT * FROM jobs WHERE enabled=TRUE AND next_run_at<=%s "
                        "ORDER BY id", (now,))
            rs = cur.fetchall()
        return [_row(r) for r in rs]

    def mark_fired(self, job_id, *, last_run_at, next_run_at,
                   last_error=None) -> None:
        last_run_at = _norm_ts(last_run_at)
        next_run_at = _norm_ts(next_run_at)
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET last_run_at=%s, next_run_at=%s, last_error=%s "
                "WHERE id=%s", (last_run_at, next_run_at, last_error, job_id))
            c.commit()


# ═══════════════════════════ backend selection ═══════════════════════════════

_BACKEND = None
_BACKEND_LOCK = threading.Lock()


def _backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            if getattr(_env, "AIFORGE_USE_SQLITE", True):
                _BACKEND = _SqliteJobStore()
            else:
                _BACKEND = _PgJobStore(_env.AIFORGE_PG_URL)
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None


# ═══════════════════════════ public function API ═════════════════════════════

def create(*, name: str, cron: str, ticket_title: str, ticket_body: str,
           project: str | None = None, next_run_at: str,
           kind: str = "ticket", script_path: str | None = None) -> dict:
    return _backend().create(
        name=name, cron=cron, ticket_title=ticket_title,
        ticket_body=ticket_body, project=project, next_run_at=next_run_at,
        kind=kind, script_path=script_path)


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


def delete(job_id: int) -> bool:
    return _backend().delete(job_id)


def due_jobs(now: str) -> list[dict]:
    """Enabled jobs whose next_run_at has passed. A job missed while the
    service was down is naturally 'due' at startup — catch-up-once falls
    out of this query plus mark_fired recomputing from *now*."""
    return _backend().due_jobs(now)


def mark_fired(job_id: int, *, last_run_at: str, next_run_at: str,
               last_error: str | None = None) -> None:
    return _backend().mark_fired(
        job_id, last_run_at=last_run_at, next_run_at=next_run_at,
        last_error=last_error)
