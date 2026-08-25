"""Embedder/dimension health, re-embed, semantic dedupe + stats."""
from __future__ import annotations

import json
import sqlite3

from aiforge_core.memory import local_embed

from ._schema import _LOCK, _conn, _db_path, _log


def stored_dim_mismatch() -> bool:
    """True if the stored embeddings' dimension differs from the ACTIVE embedder
    — i.e. a backend/model switch or a migration left rows embedded by a
    different embedder, so KNN is broken until a reembed. Cheap (samples 1 row)."""
    try:
        active = int(local_embed.embed_dim())
    except Exception:  # noqa: BLE001
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT embedding FROM memory_units WHERE embedding != '[]' LIMIT 1"
        ).fetchone()
    if not row:
        return False
    try:
        return len(json.loads(row["embedding"] or "[]")) != active
    except (TypeError, ValueError):
        return False


def _get_meta(c, key: str) -> str:
    try:
        row = c.execute("SELECT value FROM memory_meta WHERE key = ?",
                        (key,)).fetchone()
        return (row["value"] if row else "") or ""
    except sqlite3.Error:
        return ""


def stored_embedder_changed() -> bool:
    """True if the embedder that produced the stored vectors DIFFERS from the
    active one (backend or model) — even at the SAME dimension. hash and
    model2vec are both 256-dim, so a dim-only check misses hash↔model2vec and
    leaves stale vectors; this signature check catches it. Rows with no vectors
    yet (fresh store) → False."""
    active = local_embed.embed_signature()
    with _conn() as c:
        stored = _get_meta(c, "embed_sig")
        if not stored:
            # no signature yet: only a mismatch if there ARE embedded rows from a
            # prior (unknown) embedder — a fresh/empty store is not "changed".
            row = c.execute(
                "SELECT 1 FROM memory_units WHERE embedding != '[]' LIMIT 1"
            ).fetchone()
            return row is not None
        return stored != active


def reembed_all() -> dict:
    """Recompute EVERY unit's embedding with the ACTIVE embedder. Needed after a
    backend/model switch or a migration that imported rows embedded by a
    DIFFERENT embedder (mixed dims → broken KNN). The vec index rebuilds
    automatically: _conn's _init_vec recreates vec_memory at the active dim
    (dropping a stale-dim one) and the per-row UPDATE triggers repopulate it.
    Idempotent. Returns the count re-embedded."""
    n = 0
    failed = 0
    with _LOCK, _conn() as c:      # _init_vec (in _conn) already fixed the vec dim
        rows = c.execute("SELECT id, text FROM memory_units").fetchall()
        for r in rows:
            try:
                v = local_embed.embed(r["text"] or "")
            except Exception:                     # noqa: BLE001 — half-broken model
                failed += 1                       # leave the existing vector intact,
                continue                          # don't abort the whole batch
            c.execute("UPDATE memory_units SET embedding = ? WHERE id = ?",
                      (json.dumps(v), r["id"]))
            n += 1
        # stamp WHICH embedder these vectors came from, so a later backend/model
        # switch (even at the same dim) is detected + triggers another reembed.
        if not failed:
            c.execute("INSERT INTO memory_meta(key, value) VALUES('embed_sig', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (local_embed.embed_signature(),))
    if failed:
        _log.warning("reembed: %d/%d rows could not be embedded (model "
                     "unavailable); left unchanged", failed, n + failed)
    return {"reembedded": n, "failed": failed}


def _dedupe_query(repo: "str | None", max_scan: int) -> "tuple[str, tuple]":
    """The scan SQL + params for the dedupe sweep (preferences excluded; scoped
    to ``repo`` when given), newest-first."""
    where = "WHERE kind != 'preference'"
    params: tuple = ()
    if repo is not None:
        where += " AND (repo IS ? OR repo = ?)"
        params = (repo, repo)
    return (f"SELECT id, kind, embedding FROM memory_units {where} "
            "ORDER BY id DESC LIMIT ?", (*params, max_scan))


def _is_near_dup(kind: str, vec: list, kept: list, threshold: float) -> bool:
    """True when ``vec`` is within ``threshold`` cosine of an already-kept unit of
    the same ``kind``."""
    for _kid, kkind, kvec in kept:
        if kkind == kind and local_embed.cosine(vec, kvec) >= threshold:
            return True
    return False


def _row_vector(row) -> list:
    """The stored embedding of a row, or [] when absent/unparseable."""
    try:
        vec = json.loads(row["embedding"] or "[]")
    except (TypeError, ValueError):
        return []
    return vec if (vec and any(vec)) else []


def dedupe(*, repo: str | None = None, threshold: float = 0.95,
           max_scan: int = 5000) -> dict:
    """Periodic SEMANTIC dedup sweep. write_unit only dedups EXACT (repo,text);
    paraphrases ("README had 3 X" vs "README contained 3 X refs") accumulate.
    This collapses near-duplicates (cosine >= ``threshold`` on the STORED
    embeddings — no sidecar call) within the same ``kind``, keeping the NEWEST
    (highest id) and deleting the rest. Preferences (``kind='preference'``) are
    left alone (they're subject-upserted + distinct on purpose). Returns
    ``{scanned, removed}``. Best-effort — a bad row never stops the sweep."""
    with _LOCK, _conn() as c:
        sql, params = _dedupe_query(repo, max_scan)
        rows = c.execute(sql, params).fetchall()
        # rows are newest-first; keep the first of each near-duplicate cluster.
        kept: list[tuple[int, str, list]] = []
        remove: list[int] = []
        for r in rows:
            vec = _row_vector(r)
            if not vec:
                continue                     # no vector → can't compare, keep
            if _is_near_dup(r["kind"], vec, kept, threshold):
                remove.append(r["id"])
            else:
                kept.append((r["id"], r["kind"], vec))
        for rid in remove:
            c.execute("DELETE FROM memory_units WHERE id = ?", (rid,))
        return {"scanned": len(rows), "removed": len(remove)}


def stats() -> dict:
    """Counts for health / dashboard. Soft — returns zeros on error."""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
            by_kind = {
                row["kind"]: row["n"]
                for row in c.execute(
                    "SELECT kind, COUNT(*) AS n FROM memory_units GROUP BY kind"
                ).fetchall()
            }
        return {"backend": "sqlite", "total": int(total), "by_kind": by_kind,
                "db_path": _db_path()}
    except Exception:
        return {"backend": "sqlite", "total": 0, "by_kind": {},
                "db_path": _db_path()}
