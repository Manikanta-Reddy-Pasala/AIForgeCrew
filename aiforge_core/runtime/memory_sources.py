"""Registry of memory ingestion sources for the Memory Settings UI.

A "source" is something the user wants folded into memory: a code repo /
folder, a docs/markdown folder, an external URL, or an uploaded file.
Stored in SQLite (always available); the actual ingestion is done by
:mod:`aiforge_core.runtime.memory_ingest`, which writes into the embedded
SQLite memory store.

Lives at ``$AIFORGE_SOURCES_DB_PATH`` (default
``$AIFORGE_CONFIG_DIR/memory_sources.db``) — under the persisted
``app_state`` volume on the compose deploy.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from aiforge_core.config.paths import config_dir

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
    detail       TEXT,
    last_indexed TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_SOURCES_DB_PATH",
        os.path.join(
            str(config_dir()),
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
        _migrate(c)
        yield c
        c.commit()
    finally:
        c.close()


def _migrate(c: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema to pre-existing DBs
    (CREATE TABLE IF NOT EXISTS won't retro-add them). Idempotent."""
    cols = {r[1] for r in c.execute(
        "PRAGMA table_info(memory_sources)").fetchall()}
    if "detail" not in cols:
        c.execute("ALTER TABLE memory_sources ADD COLUMN detail TEXT")
    if "indexing_started_at" not in cols:
        c.execute(
            "ALTER TABLE memory_sources ADD COLUMN indexing_started_at TEXT")


def _iso(v):
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return v
    return v


def _row(r: sqlite3.Row) -> dict:
    keys = r.keys()
    return {"id": r["id"], "kind": r["kind"], "name": r["name"],
            "location": r["location"], "status": r["status"],
            "units": r["units"], "error": r["error"],
            "detail": (r["detail"] if "detail" in keys else None),
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


def reset_all() -> int:
    """Reset every registered source's index STATE (status→idle, units→0,
    clear error/last_indexed) WITHOUT deleting the registration itself.

    Used by the memory admin "clear data" actions: the indexed nodes/units are
    wiped from the backend, but the user's registered repos/dirs survive so they
    can simply be re-indexed to repopulate. Returns the count of sources reset.
    """
    with _LOCK, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0]
        c.execute(
            "UPDATE memory_sources SET status='idle', units=0, error=NULL, "
            "last_indexed=NULL, detail=NULL WHERE status!='indexing'"
        )
    return int(n or 0)


def status_counts() -> dict:
    """``{status: count}`` across all registered sources. Soft — {} on error."""
    try:
        with _conn() as c:
            return {
                r["status"]: r["n"]
                for r in c.execute(
                    "SELECT status, COUNT(*) AS n FROM memory_sources "
                    "GROUP BY status"
                ).fetchall()
            }
    except Exception:  # noqa: BLE001
        return {}


def claim_for_index(source_id: int) -> bool:
    """Atomically flip a source into ``indexing`` and stamp
    ``indexing_started_at``. Returns True only when THIS caller won the flip
    (the row existed and was NOT already ``indexing``); returns False when the
    source is already ``indexing`` (a concurrent index is in-flight) so the
    caller must NOT re-spawn an ingest thread. The single-writer SQLite lock
    makes the check-and-set race-free across the API's index endpoints."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            f"UPDATE memory_sources SET status='indexing', "
            f"indexing_started_at={_NOW}, error=NULL "
            "WHERE id=? AND status!='indexing'",
            (source_id,),
        )
        return (cur.rowcount or 0) > 0


def reap_stale_indexing(max_age_s: int) -> list[int]:
    """Reset sources stuck ``indexing`` past the lease back to ``idle`` (a
    crashed ingest thread never clears its own terminal status). Falls back to
    ``created_at`` for rows predating the ``indexing_started_at`` column.
    Meant to run at boot. Returns the reset source ids."""
    cutoff = "strftime('%Y-%m-%dT%H:%M:%fZ','now',?)"
    arg = f"-{int(max_age_s)} seconds"
    reset: list[int] = []
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT id FROM memory_sources WHERE status='indexing' "
            f"AND COALESCE(indexing_started_at, created_at) < {cutoff}",
            (arg,),
        ).fetchall()
        for r in rows:
            upd = c.execute(
                "UPDATE memory_sources SET status='idle', "
                "error='reset: indexing exceeded lease' "
                "WHERE id=? AND status='indexing'",
                (r["id"],),
            )
            if upd.rowcount and upd.rowcount > 0:
                reset.append(int(r["id"]))
    return reset


def touch_indexing(source_id: int) -> None:
    """Heartbeat: bump ``indexing_started_at`` to now while a source is STILL
    actively indexing, so ``reap_stale_indexing`` only reaps a genuinely
    stalled index — not a slow-but-progressing one (big repos on slow
    filesystems, e.g. WSL /mnt/c, legitimately exceed the lease). No-op unless
    the row is currently 'indexing', so it can never revive a finished/crashed
    one."""
    with _conn() as c:
        c.execute(
            f"UPDATE memory_sources SET indexing_started_at={_NOW} "
            "WHERE id=? AND status='indexing'",
            (source_id,),
        )


def set_status(source_id: int, status: str, *, units: int | None = None,
               error: str | None = None, indexed: bool = False,
               layers: dict | None = None) -> None:
    sets = ["status = ?"]
    params: list = [status]
    if status == "indexing":
        # (Re)entering 'indexing' restarts the lease clock, so the stale-index
        # reaper measures from THIS start — not a stale prior timestamp that
        # would get it reaped almost immediately.
        sets.append(f"indexing_started_at = {_NOW}")
    if units is not None:
        sets.append("units = ?")
        params.append(units)
    sets.append("error = ?")
    params.append(error)
    if layers is not None:
        # Per-layer index outcome (code_chunks/doc_chunks/symbols/graphify →
        # ok|skip:…|error:…) so the UI/operator can see which layer failed on
        # a "partial" index. Stored as JSON; soft — a serialize failure just
        # skips the column.
        try:
            sets.append("detail = ?")
            params.append(json.dumps(layers))
        except (TypeError, ValueError):
            sets.pop()
    if indexed:
        sets.append(f"last_indexed = {_NOW}")
    params.append(source_id)
    with _conn() as c:
        c.execute(f"UPDATE memory_sources SET {', '.join(sets)} WHERE id = ?",
                  params)
