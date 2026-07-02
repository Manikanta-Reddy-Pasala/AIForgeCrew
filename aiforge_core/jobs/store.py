"""Scheduled-jobs store — single-file SQLite, `runtime/chat_store.py`
pattern (module DDL, WAL, context-manager connection). Jobs are small
operator-local scheduling state, like chat sessions — the tickets
store's dual-backend machinery is deliberately NOT used here.

Path: $AIFORGE_JOBS_DB_PATH, default $AIFORGE_CONFIG_DIR/jobs.db —
under the compose ``app_state`` volume so jobs survive redeploys.

Timestamps are naive server-LOCAL ISO-8601 strings (second precision) —
matching the spec's cron semantics ("8am" = local 8am). Lexicographic
comparison == chronological, with one accepted edge: during a DST
fall-back hour the wall clock (and thus the string) repeats; the
recompute-from-now in mark_fired prevents double-fires, so the worst
case is a fire shifted by up to an hour once a year.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

_LOCK = threading.Lock()

_DDL = """
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
  created_at TEXT NOT NULL
);
"""

_UPDATABLE = {"name", "cron", "ticket_title", "ticket_body", "project",
              "enabled", "next_run_at", "last_error"}


def _db_path() -> str:
    raw = os.environ.get("AIFORGE_JOBS_DB_PATH")
    if raw:
        return os.path.expanduser(raw)
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")
    return os.path.join(os.path.expanduser(cfg), "jobs.db")


@contextmanager
def _conn():
    path = _db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_DDL)
        yield con
        con.commit()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_ts(ts: str) -> str:
    """Normalize any ISO-8601-ish timestamp to the store's canonical
    bare-seconds local format — the lexicographic due-query depends on
    every stored timestamp sharing this exact shape."""
    return datetime.fromisoformat(ts).replace(tzinfo=None) \
        .isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return d


def create(*, name: str, cron: str, ticket_title: str, ticket_body: str,
           project: str | None = None, next_run_at: str) -> dict:
    next_run_at = _norm_ts(next_run_at)
    with _LOCK, _conn() as con:
        cur = con.execute(
            "INSERT INTO jobs (name, cron, ticket_title, ticket_body, "
            "project, next_run_at, created_at) VALUES (?,?,?,?,?,?,?)",
            (name, cron, ticket_title, ticket_body, project,
             next_run_at, now_iso()))
        r = con.execute("SELECT * FROM jobs WHERE id=?",
                        (cur.lastrowid,)).fetchone()
        return _row(r)


def get(job_id: int) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row(r) if r else None


def list_jobs() -> list[dict]:
    with _conn() as con:
        rs = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [_row(r) for r in rs]


def update(job_id: int, **fields) -> dict | None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"unknown job fields: {sorted(bad)}")
    if not fields:
        return get(job_id)
    if fields.get("next_run_at") is not None:
        fields["next_run_at"] = _norm_ts(fields["next_run_at"])
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    with _LOCK, _conn() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*vals, job_id))
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row(r) if r else None


def delete(job_id: int) -> bool:
    with _LOCK, _conn() as con:
        cur = con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return cur.rowcount > 0


def due_jobs(now: str) -> list[dict]:
    """Enabled jobs whose next_run_at has passed. A job missed while the
    service was down is naturally 'due' at startup — catch-up-once falls
    out of this query plus mark_fired recomputing from *now*."""
    with _conn() as con:
        rs = con.execute(
            "SELECT * FROM jobs WHERE enabled=1 AND next_run_at<=? "
            "ORDER BY id", (now,)).fetchall()
        return [_row(r) for r in rs]


def mark_fired(job_id: int, *, last_run_at: str, next_run_at: str,
               last_error: str | None = None) -> None:
    last_run_at = _norm_ts(last_run_at)
    next_run_at = _norm_ts(next_run_at)
    with _LOCK, _conn() as con:
        con.execute(
            "UPDATE jobs SET last_run_at=?, next_run_at=?, last_error=? "
            "WHERE id=?", (last_run_at, next_run_at, last_error, job_id))
