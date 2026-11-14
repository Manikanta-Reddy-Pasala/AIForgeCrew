"""Remove vec0 index rows whose memory unit is gone, then VACUUM (best-effort).

Why rows go stale: while sqlite-vec is not loaded on a connection the vec0
sync triggers are dropped (``_schema._drop_vec_triggers``) so writes still
land; deletes made in that window never reach ``vec_memory``, and the
incremental backfill in ``_init_vec`` only ADDS. On nuc (11-14) the vec index
held 875 rows for 415 memory units. Orphans waste space and, worse, take KNN
slots: a hit whose unit is gone is dropped after the search, so recall returns
fewer results than asked for.

Off switch: ``AIFORGE_COMPACT_VEC_PRUNE=0``.
"""
from __future__ import annotations

import os
import sqlite3

from ._schema import _LOCK, _db_path, _log


def _load_vec(c) -> bool:
    try:
        import sqlite_vec
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        return True
    except Exception as exc:  # noqa: BLE001 — no extension: nothing we can do
        _log.info("vec prune: sqlite-vec not loadable (%s) — skipped", exc)
        return False


def _vacuum(path: str) -> str:
    """VACUUM only when nobody else holds a write: a short busy timeout, and a
    lock error means 'try next pass', never wait or fail the step."""
    try:
        c = sqlite3.connect(path, timeout=2.0)
        try:
            c.execute("PRAGMA busy_timeout=2000")
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        return "done"
    except sqlite3.OperationalError as exc:
        _log.info("vec prune: VACUUM skipped (%s)", exc)
        return f"skipped: {exc}"


def prune_vec_orphans(*, vacuum: bool = True) -> dict:
    """Delete ``vec_memory`` rows with no ``memory_units`` row. Never raises."""
    if os.environ.get("AIFORGE_COMPACT_VEC_PRUNE", "1") == "0":
        return {"ok": True, "skipped": "disabled"}
    path = _db_path()
    if not os.path.exists(path):
        return {"ok": True, "skipped": "no database"}
    out: dict = {"ok": True, "removed": 0}
    try:
        with _LOCK:
            c = sqlite3.connect(path, timeout=5.0)
            try:
                if not c.execute("SELECT 1 FROM sqlite_master WHERE name = "
                                 "'vec_memory'").fetchone():
                    return {"ok": True, "skipped": "no vec table"}
                if not _load_vec(c):
                    return {"ok": True, "skipped": "sqlite-vec unavailable"}
                units = {r[0] for r in c.execute("SELECT id FROM memory_units")}
                vec = [r[0] for r in c.execute("SELECT rowid FROM vec_memory")]
                orphans = [r for r in vec if r not in units]
                out.update(units=len(units), vec_rows=len(vec))
                if orphans:
                    c.execute("BEGIN IMMEDIATE")
                    for rid in orphans:
                        c.execute("DELETE FROM vec_memory WHERE rowid = ?", (rid,))
                    c.commit()
                out["removed"] = len(orphans)
            finally:
                c.close()
    except sqlite3.OperationalError as exc:   # locked by a writer: next pass
        return {"ok": True, "skipped": f"busy: {exc}", "removed": 0}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if out["removed"]:
        _log.info("vec prune: removed %d orphan vec row(s)", out["removed"])
        if vacuum:
            out["vacuum"] = _vacuum(path)
    return out
