"""Embedded SQLite memory store — zero-infra observation/recall.

Stores memory units (the agent's learnings, failures, self-writes) in
``~/.aiforge/memory.db`` with an offline hash embedding, and recalls by
brute-force cosine. Used when ``backend_select.embedded()`` is True
(no Neo4j / Postgres configured). Quality is lexical, not semantic —
the "pro" Neo4j/bge-m3 path supersedes it when its env vars are set.

Public surface:
    write_unit(*, text, kind, ...) -> int        # 0 when skipped (dup/empty)
    recall(text, *, limit, repo) -> list[dict]
    stats() -> dict
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from aiforge_core.memory import local_embed

_LOCK = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS memory_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'observation',
    wing        TEXT,
    source      TEXT,
    title       TEXT,
    text        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}',
    repo        TEXT,
    ticket      TEXT,
    embedding   TEXT NOT NULL DEFAULT '[]',
    event_time  REAL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS memory_units_repo ON memory_units(repo);
CREATE INDEX IF NOT EXISTS memory_units_kind ON memory_units(kind);
CREATE INDEX IF NOT EXISTS memory_units_ticket ON memory_units(ticket);
"""


def _db_path() -> str:
    return os.environ.get(
        "AIFORGE_MEMORY_DB_PATH",
        os.path.join(
            os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")),
            "memory.db",
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


def write_unit(
    *,
    text: str,
    kind: str = "observation",
    wing: str | None = None,
    source: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    repo: str | None = None,
    ticket: str | None = None,
    event_time: float | None = None,
) -> int:
    """Insert a memory unit, returning its row id (0 if skipped).

    Skips empty text and exact ``(repo, text)`` duplicates so repeated
    runs don't bloat the store — mirrors the AFM pure-text dedupe.
    """
    text = (text or "").strip()
    if not text:
        return 0
    vec = local_embed.embed(text)
    with _LOCK, _conn() as c:
        dup = c.execute(
            "SELECT id FROM memory_units WHERE text = ? AND "
            "(repo IS ? OR repo = ?) LIMIT 1",
            (text, repo, repo),
        ).fetchone()
        if dup:
            return 0
        cur = c.execute(
            "INSERT INTO memory_units "
            "(kind, wing, source, title, text, tags, metadata, repo, ticket, "
            " embedding, event_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind, wing, source, title, text,
                json.dumps(tags or []), json.dumps(metadata or {}),
                repo, ticket, json.dumps(vec), event_time,
            ),
        )
        return int(cur.lastrowid)


def delete_by_tag(tag: str, *, repo: str | None = None) -> int:
    """Delete every unit carrying ``tag`` (exact tag match in the JSON tags
    array). When ``repo`` is given, scope to that repo plus repo-agnostic rows;
    else all repos. Returns the count removed. Backs preference upsert."""
    tag = (tag or "").strip()
    if not tag:
        return 0
    needle = json.dumps(tag)   # match the tag as a JSON string element
    with _LOCK, _conn() as c:
        if repo is None:
            rows = c.execute("SELECT id, tags FROM memory_units").fetchall()
        else:
            rows = c.execute(
                "SELECT id, tags FROM memory_units WHERE repo IS ? OR repo = ?",
                (repo, repo)).fetchall()
        ids = []
        for r in rows:
            try:
                if tag in (json.loads(r["tags"] or "[]") or []):
                    ids.append(r["id"])
            except (TypeError, ValueError):
                continue
        for i in ids:
            c.execute("DELETE FROM memory_units WHERE id = ?", (i,))
        return len(ids)


def upsert_by_tag(*, text: str, tag: str, kind: str = "learning",
                  source: str | None = None, tags: list[str] | None = None,
                  metadata: dict | None = None, repo: str | None = None) -> int:
    """Replace-in-place: remove any prior unit carrying ``tag`` (same-topic
    memory) then write the new one — so a RESTATED preference UPDATES the
    existing memory instead of piling up a contradictory duplicate. ``tag`` is
    always added to the stored tags. Returns the new row id."""
    all_tags = list(dict.fromkeys([tag, *(tags or [])]))
    delete_by_tag(tag, repo=repo)
    return write_unit(text=text, kind=kind, source=source, tags=all_tags,
                      metadata=metadata, repo=repo)


def recall(text: str, *, limit: int = 8, repo: str | None = None,
           boost_tags: list[str] | None = None) -> list[dict]:
    """Brute-force cosine recall. Returns hits sorted by score desc.

    Each hit: ``{text, title, source, kind, ticket, repo, score}`` with
    ``score`` the clamped cosine in [0, 1]. ``repo`` filters to that
    repo plus repo-agnostic rows when provided.

    ``boost_tags``: rows whose stored ``tags`` intersect this set get a fixed
    score bump — so a tool-scoped learning (e.g. ``tool:jira``) reliably
    surfaces when that tool is in play, even on a differently-worded but
    same-type request that pure semantics would rank below noise.
    """
    text = (text or "").strip()
    if not text or limit <= 0:
        return []
    boost = {t.lower() for t in (boost_tags or []) if t}
    qvec = local_embed.embed(text)
    if not any(qvec):
        return []
    with _conn() as c:
        if repo:
            # GLOBAL knowledge (stored under repo='shared') and repo-agnostic
            # rows (NULL) are always available to a repo-scoped recall — that's
            # what makes global memory reachable for every project.
            rows = c.execute(
                "SELECT * FROM memory_units "
                "WHERE repo = ? OR repo IS NULL OR repo = 'shared'",
                (repo,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM memory_units").fetchall()
    scored: list[dict] = []
    for r in rows:
        try:
            vec = json.loads(r["embedding"] or "[]")
        except (TypeError, ValueError):
            continue
        score = local_embed.cosine(qvec, vec)
        if score <= 0.0:
            continue
        if boost:
            try:
                row_tags = {str(t).lower() for t in json.loads(r["tags"] or "[]")}
            except (TypeError, ValueError):
                row_tags = set()
            if row_tags & boost:
                score = min(1.0, score + 0.3)   # tool-scoped learning wins ties
        scored.append({
            "text": r["text"],
            "title": r["title"],
            "source": r["source"] or "memory",
            # Per-item group so unified_query._diversify diversifies multi-item
            # recall by row (file/id) rather than squashing every row to the
            # single shared source="doer" group. Mirrors afm chunks' distinct
            # "afm:chunk:{path}" groups.
            "group": f"sqlite:{r['id']}",
            "kind": r["kind"],
            "ticket": r["ticket"],
            "repo": r["repo"],
            "score": max(0.0, min(1.0, score)),
        })
    scored.sort(key=lambda h: -h["score"])
    # Dedup by text (keep the highest score) — the same learning can exist
    # both repo-scoped and repo-agnostic (write_unit dedup is per-(repo,text)),
    # and recall unions repo + NULL rows, so identical text would surface twice.
    seen: set[str] = set()
    out: list[dict] = []
    for h in scored:
        key = h["text"]
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def delete_stale_compacted_notes() -> int:
    """Reclaim STALE brief index rows so a brief isn't stored twice:
      * compacted rows stranded under ``repo='notes'`` (pre-scope-fix default);
      * the old ``ingest_dir`` brief rows keyed ``source='md:compacted-…'``
        (before ingest_dir mirrored Phase-3's ``kind=compacted``/``compacted:…``).
    Both are re-created cleanly by the ensuing ingest under the unified
    kind+source. Idempotent (0 once clean). Returns count removed."""
    with _LOCK, _conn() as c:
        cur = c.execute(
            "DELETE FROM memory_units WHERE (kind = 'compacted' AND repo = 'notes')"
            " OR source LIKE 'md:compacted-%'")
        return cur.rowcount or 0


def prune_missing_file_rows(present_sources) -> int:
    """Delete file-backed index rows (``source`` ``md:*`` / ``compacted:*``) whose
    md file is GONE — keeps the vector index in sync when an md is deleted or
    archived (md is the source of truth). ``present_sources`` = the sources of
    the files currently on disk. Rows from non-file origins (chat-session:,
    migrate:, capture, rule …) are untouched. Returns count removed."""
    present = set(present_sources or [])
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT id, source FROM memory_units "
            "WHERE source LIKE 'md:%' OR source LIKE 'compacted:%'").fetchall()
        ids = [r["id"] for r in rows if r["source"] not in present]
        for i in ids:
            c.execute("DELETE FROM memory_units WHERE id = ?", (i,))
        return len(ids)


def delete_by_source(source: str) -> int:
    """Delete every unit with this exact ``source``. Used to reclaim a brief's
    PRIOR index generation before re-ingesting the new one — otherwise each
    recompaction that changes a brief mints a new row and the old ones (incl.
    pre-scope-fix ``repo='notes'`` copies) pile up forever. Returns count removed."""
    source = (source or "").strip()
    if not source:
        return 0
    with _LOCK, _conn() as c:
        cur = c.execute("DELETE FROM memory_units WHERE source = ?", (source,))
        return cur.rowcount or 0


def delete_by_text_contains(fragment: str, *, repo: str,
                            exclude_kind: str | None = None) -> int:
    """Delete units under ``repo`` whose stored text CONTAINS ``fragment``.
    Used when a fact is MOVED between scopes (reheal promotion) so the stale
    row doesn't linger under the old repo and duplicate the moved copy. Repo is
    required (never a blanket delete). ``exclude_kind`` skips rows of that kind —
    pass ``"compacted"`` so a fact fragment can't match (and delete) the whole
    consolidated brief row, which contains every fact. Returns the count removed."""
    frag = (fragment or "").strip()
    repo = (repo or "").strip()
    if not frag or not repo:
        return 0
    with _LOCK, _conn() as c:
        rows = c.execute(
            "SELECT id, text, kind FROM memory_units WHERE repo = ?",
            (repo,)).fetchall()
        ids = [r["id"] for r in rows if frag in (r["text"] or "")
               and (exclude_kind is None or r["kind"] != exclude_kind)]
        for i in ids:
            c.execute("DELETE FROM memory_units WHERE id = ?", (i,))
        return len(ids)


def dedupe(*, repo: str | None = None, threshold: float = 0.95,
           max_scan: int = 5000) -> dict:
    """Periodic SEMANTIC dedup sweep. write_unit only dedups EXACT (repo,text);
    paraphrases ("README had 3 X" vs "README contained 3 X refs") accumulate.
    This collapses near-duplicates (cosine ≥ ``threshold`` on the STORED
    embeddings — no sidecar call) within the same ``kind``, keeping the NEWEST
    (highest id) and deleting the rest. Preferences (``kind='preference'``) are
    left alone (they're subject-upserted + distinct on purpose). Returns
    ``{scanned, removed}``. Best-effort — a bad row never stops the sweep."""
    with _LOCK, _conn() as c:
        where = "WHERE kind != 'preference'"
        params: tuple = ()
        if repo is not None:
            where += " AND (repo IS ? OR repo = ?)"
            params = (repo, repo)
        rows = c.execute(
            f"SELECT id, kind, embedding FROM memory_units {where} "
            "ORDER BY id DESC LIMIT ?", (*params, max_scan)).fetchall()
        # rows are newest-first; keep the first of each near-duplicate cluster.
        kept: list[tuple[int, str, list]] = []
        remove: list[int] = []
        for r in rows:
            try:
                vec = json.loads(r["embedding"] or "[]")
            except (TypeError, ValueError):
                vec = []
            if not vec or not any(vec):
                continue                     # no vector → can't compare, keep
            dup = False
            for _kid, kkind, kvec in kept:
                if kkind != r["kind"]:
                    continue
                if local_embed.cosine(vec, kvec) >= threshold:
                    dup = True
                    break
            if dup:
                remove.append(r["id"])
            else:
                kept.append((r["id"], r["kind"], vec))
        for rid in remove:
            c.execute("DELETE FROM memory_units WHERE id = ?", (rid,))
        return {"scanned": len(rows), "removed": len(remove)}


def clear() -> int:
    """Delete every memory unit and reset the id sequence. Returns the count
    of rows removed. Idempotent — a second call returns 0. Used by the memory
    admin "empty this store" action."""
    with _LOCK, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
        c.execute("DELETE FROM memory_units")
        c.execute("DELETE FROM sqlite_sequence WHERE name='memory_units'")
    return int(n or 0)


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
