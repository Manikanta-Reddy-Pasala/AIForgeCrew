"""The last runs of each scheduled job: when it fired, how long it took, and
whether it went well — so the progress panel can show a schedule is healthy,
not only when it runs next.

The jobs row keeps one ``last_run_at`` / ``last_error``; every new fire
overwrites them. This keeps the last ``KEEP`` outcomes per job in their own
table (``$AIFORGE_CONFIG_DIR/job_runs.db``, ``AIFORGE_JOB_RUNS_DB_PATH``).
Best-effort: a write that fails never touches the run itself.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime

log = logging.getLogger("aiforge.jobs.run_log")

KEEP = 20
_LOCK = threading.Lock()
_DONE: set = set()
_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  started_at TEXT,
  ended_at TEXT NOT NULL,
  ok INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job_id, id);
"""


def _path() -> str:
    """Next to the jobs table it belongs to (a fresh jobs db restarts its ids:
    another db's history must not show under them)."""
    raw = os.environ.get("AIFORGE_JOB_RUNS_DB_PATH")
    if raw:
        return os.path.expanduser(raw)
    try:
        from aiforge_core.jobs import store
        jobs_db = store._backend()._db_path()
        return os.path.join(os.path.dirname(jobs_db) or ".", "job_runs.db")
    except Exception:  # noqa: BLE001 — another backend: the config dir
        from aiforge_core.config.paths import config_dir
        return os.path.join(os.path.expanduser(str(config_dir())), "job_runs.db")


def _connect():
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fresh = not os.path.exists(path)          # deleted since: its table too
    con = sqlite3.connect(path, timeout=5.0)
    con.row_factory = sqlite3.Row
    if path not in _DONE or fresh:
        con.executescript(_DDL)
        _DONE.add(path)
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(job_id, *, started_at=None, ok: "bool | None", note: "str | None" = None) -> None:
    """One finished (or skipped) fire of ``job_id``. ``ok`` None: neither —
    stopped by the user, or the outcome is not known."""
    try:
        with _LOCK:
            con = _connect()
            try:
                con.execute("INSERT INTO job_runs (job_id, started_at, ended_at, ok, note) "
                            "VALUES (?,?,?,?,?)",
                            (int(job_id), started_at, _now(), None if ok is None else (1 if ok else 0),
                             (str(note)[:300] if note else None)))
                con.execute("DELETE FROM job_runs WHERE job_id=? AND id NOT IN "
                            "(SELECT id FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?)",
                            (int(job_id), int(job_id), KEEP))
                con.commit()
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001 — never touches the run
        log.debug("job run not logged: %s", exc)


def recent(job_id, n: int = 5) -> list:
    """The last ``n`` runs of ``job_id``, newest first."""
    try:
        con = _connect()
        try:
            rows = con.execute("SELECT started_at, ended_at, ok, note FROM job_runs "
                               "WHERE job_id=? ORDER BY id DESC LIMIT ?",
                               (int(job_id), int(n))).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("job runs not read: %s", exc)
        return []


__all__ = ["KEEP", "record", "recent"]
