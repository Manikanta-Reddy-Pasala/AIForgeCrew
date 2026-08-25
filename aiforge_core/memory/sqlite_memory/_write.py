"""Write / upsert / delete mutations on the SQLite memory store."""
from __future__ import annotations

import json

from ._schema import _LOCK, _conn, _safe_embed

_DELETE_FROM_MEMORY_UNITS_WHE = 'DELETE FROM memory_units WHERE id = ?'


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
    vec = _safe_embed(text)
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
            c.execute(_DELETE_FROM_MEMORY_UNITS_WHE, (i,))
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
            " OR source LIKE 'md:compacted-%'"
            " OR (source LIKE 'compacted:%' AND repo = 'notes')")
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
            c.execute(_DELETE_FROM_MEMORY_UNITS_WHE, (i,))
        return len(ids)


def source_text_unchanged(source: str, text: str) -> bool:
    """True if a row for ``source`` already holds exactly this ``text`` — lets a
    re-ingest SKIP the delete+re-embed when nothing changed (embed-on-change)."""
    source = (source or "").strip()
    text = (text or "").strip()
    if not source or not text:
        return False
    with _LOCK, _conn() as c:
        row = c.execute(
            "SELECT 1 FROM memory_units WHERE source = ? AND text = ? LIMIT 1",
            (source, text)).fetchone()
        return row is not None


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
            c.execute(_DELETE_FROM_MEMORY_UNITS_WHE, (i,))
        return len(ids)


def clear() -> int:
    """Delete every memory unit and reset the id sequence. Returns the count
    of rows removed. Idempotent — a second call returns 0. Used by the memory
    admin "empty this store" action."""
    with _LOCK, _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0]
        c.execute("DELETE FROM memory_units")
        c.execute("DELETE FROM sqlite_sequence WHERE name='memory_units'")
    return int(n or 0)
