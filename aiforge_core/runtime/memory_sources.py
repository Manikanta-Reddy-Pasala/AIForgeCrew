"""Registry of memory ingestion sources for the Memory Settings UI.

A "source" is something the user wants folded into memory: a code repo /
folder, a docs/markdown folder, an external URL, or an uploaded file.
Stored in SQLite (always available); the actual ingestion is done by
:mod:`aiforge_core.runtime.memory_ingest`, which writes into whatever
memory backend is active (Neo4j or embedded SQLite).

Lives at ``$AIFORGE_SOURCES_DB_PATH`` (default
``$AIFORGE_CONFIG_DIR/memory_sources.db``) — under the persisted
``app_state`` volume on the compose deploy.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

_LOCK = threading.Lock()

KINDS = {"repo", "docs", "url", "file"}

_DDL = """
CREATE TABLE IF NOT EXISTS memory_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    location     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'idle',
    units        INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    last_indexed TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_SOURCES_DB_PATH",
        os.path.join(
            os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
            "memory_sources.db",
        ),
    )


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.executescript(_DDL)
        yield c
        c.commit()
    finally:
        c.close()


def _iso(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return v
    return v


def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "kind": r["kind"], "name": r["name"],
            "location": r["location"], "status": r["status"],
            "units": r["units"], "error": r["error"],
            "last_indexed": _iso(r["last_indexed"]),
            "created_at": _iso(r["created_at"])}


def create(kind: str, location: str, name: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"bad kind: {kind}")
    if not location or not location.strip():
        raise ValueError("location required")
    name = (name or "").strip() or os.path.basename(location.rstrip("/")) or location
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO memory_sources(kind, name, location) VALUES (?,?,?)",
            (kind, name, location.strip()),
        )
        r = c.execute("SELECT * FROM memory_sources WHERE id=?",
                      (cur.lastrowid,)).fetchone()
    return _row(r)


def list_sources() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM memory_sources ORDER BY id DESC").fetchall()
    return [_row(r) for r in rows]


def get(source_id: int) -> "dict | None":
    with _conn() as c:
        r = c.execute("SELECT * FROM memory_sources WHERE id=?",
                      (source_id,)).fetchone()
    return _row(r) if r else None


def delete(source_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM memory_sources WHERE id=?", (source_id,))
    return cur.rowcount > 0


def set_status(source_id: int, status: str, *, units: int | None = None,
               error: str | None = None, indexed: bool = False) -> None:
    sets = ["status = ?"]
    params: list = [status]
    if units is not None:
        sets.append("units = ?")
        params.append(units)
    sets.append("error = ?")
    params.append(error)
    if indexed:
        sets.append(f"last_indexed = {_NOW}")
    params.append(source_id)
    with _conn() as c:
        c.execute(f"UPDATE memory_sources SET {', '.join(sets)} WHERE id = ?",
                  params)
