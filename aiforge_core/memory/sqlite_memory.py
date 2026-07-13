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

# Keyword (BM25) search — an FTS5 external-content index over memory_units.text,
# kept in sync by triggers. Gives exact-token / prefix recall (ticket ids,
# service names, hashes) that embeddings blur, fused with vector recall. Wrapped
# so a build without FTS5 degrades to vector-only instead of crashing.
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, content='memory_units', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_units_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_ad AFTER DELETE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_units_au AFTER UPDATE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO memory_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


# sqlite-vec ANN index — the production vector path (AIFORGE_EMBED_BACKEND=
# semantic). vec0 virtual table mirrors memory_units.embedding via triggers;
# recall does a real KNN instead of an O(N) Python cosine scan. NO fallback:
# when the semantic backend is selected the extension is REQUIRED (recall raises
# if it's missing) — the Python-cosine path below runs only under the dev/test
# 'hash' backend.
_VEC_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS vec_memory_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO vec_memory(rowid, embedding) VALUES (new.id, new.embedding);
END;
CREATE TRIGGER IF NOT EXISTS vec_memory_ad AFTER DELETE ON memory_units BEGIN
    DELETE FROM vec_memory WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS vec_memory_au AFTER UPDATE ON memory_units BEGIN
    DELETE FROM vec_memory WHERE rowid = old.id;
    INSERT INTO vec_memory(rowid, embedding) VALUES (new.id, new.embedding);
END;
"""


def _vec_enabled() -> bool:
    return os.environ.get("AIFORGE_EMBED_BACKEND", "hash").strip().lower() in (
        "semantic", "st", "sentence-transformers")


def _init_vec(c) -> None:
    """Load sqlite-vec + create the vec0 table (dim = active embedder) + sync
    triggers, backfilling existing rows. Raises so a broken semantic setup is
    LOUD (no silent cosine fallback)."""
    import sqlite_vec
    from aiforge_core.memory import local_embed
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    dim = int(local_embed.embed_dim())
    # If a vec_memory table exists at a DIFFERENT dimension (backend/model
    # switched, or a migration imported rows from another embedder), DROP it so
    # it's recreated at the active dim — else every insert would dim-mismatch and
    # KNN would silently return nothing.
    existing = c.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'vec_memory'").fetchone()
    if existing and existing[0] and f"float[{dim}]" not in existing[0]:
        c.execute("DROP TABLE vec_memory")
    c.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING "
        f"vec0(embedding float[{dim}] distance_metric=cosine)")
    c.executescript(_VEC_TRIGGERS)
    vn = c.execute("SELECT count(*) FROM vec_memory").fetchone()[0]
    un = c.execute("SELECT count(*) FROM memory_units").fetchone()[0]
    if un and vn < un:
        # INCREMENTAL backfill — insert only rows MISSING from the vec index, no
        # destructive DELETE-all rebuild. So concurrent readers (which enter
        # _conn without _LOCK) don't race on a table-wide delete, and a row that
        # can't be inserted doesn't force a full re-scan every connection — the
        # NOT IN gap just shrinks to it and skips otherwise.
        for r in c.execute(
                "SELECT id, embedding FROM memory_units "
                "WHERE id NOT IN (SELECT rowid FROM vec_memory)").fetchall():
            try:
                c.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                          (r["id"], r["embedding"]))
            except sqlite3.OperationalError:
                continue                   # unbackfillable row → leave it out


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
        try:
            c.executescript(_FTS_DDL)          # FTS5 may be unavailable → soft
        except sqlite3.OperationalError:
            pass
        if _vec_enabled():                     # sqlite-vec ANN (semantic backend)
            _init_vec(c)                       # RAISES if the extension is missing
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


import re as _re  # noqa: E402

_STOP = {"the", "and", "for", "with", "that", "this", "how", "why", "what",
         "are", "was", "our", "you", "your", "from", "into", "not"}
_VOCAB_CACHE: dict = {}


def _kw_tokens(q: str) -> list[str]:
    toks = [t for t in _re.findall(r"[a-z0-9][a-z0-9_-]{1,}", (q or "").lower())
            if t not in _STOP]
    # keep order, dedupe
    seen: set[str] = set()
    return [t for t in toks if not (t in seen or seen.add(t))]


def _vocab(c) -> set:
    """Cached distinct word tokens for spell correction (per db, 5-min TTL)."""
    import time
    dbp = _db_path()
    ent = _VOCAB_CACHE.get(dbp)
    if ent and time.time() - ent[0] < 300:
        return ent[1]
    v: set = set()
    try:
        for (txt,) in c.execute("SELECT text FROM memory_units"):
            for w in _re.findall(r"[a-z0-9]{3,}", (txt or "").lower()):
                v.add(w)
    except sqlite3.OperationalError:
        return set()
    _VOCAB_CACHE[dbp] = (time.time(), v)
    return v


def _spell_correct(c, toks: list[str]) -> list[str]:
    import difflib
    vocab = _vocab(c)
    if not vocab:
        return toks
    out = []
    for t in toks:
        if t in vocab or len(t) < 4:
            out.append(t)
            continue
        m = difflib.get_close_matches(t, vocab, n=1, cutoff=0.82)
        out.append(m[0] if m else t)
    return out


def _fts_rows(c, toks: list[str], repo, limit: int) -> list:
    terms = []
    for t in toks:
        parts = [p for p in _re.split(r"[^a-z0-9]+", t) if p]
        if not parts:
            continue
        if len(parts) == 1:
            terms.append(f"{parts[0]}*")                # single word → prefix
        else:
            # a hyphenated token (ONE-3, sha-256) tokenizes as adjacent words in
            # FTS → match it as a phrase so "one-3" finds "ONE-3".
            terms.append('"' + " ".join(parts) + '"')
    if not terms:
        return []
    match = " OR ".join(terms)
    where = ("(u.repo = ? OR u.repo IS NULL OR u.repo = 'shared')"
             if repo else "1=1")
    params = [match] + ([repo] if repo else []) + [limit]
    try:
        return c.execute(
            "SELECT u.*, bm25(memory_fts) AS _bm FROM memory_fts f "
            "JOIN memory_units u ON u.id = f.rowid "
            f"WHERE memory_fts MATCH ? AND {where} ORDER BY _bm LIMIT ?",
            params).fetchall()
    except sqlite3.OperationalError:
        return []


def keyword_search(query: str, *, repo: str | None = None,
                   limit: int = 8) -> list[dict]:
    """BM25 keyword recall over ``memory_units.text`` (FTS5) — exact-token /
    prefix matching that embeddings blur (ticket ids, hashes, service names),
    with automatic SPELL CORRECTION: if the raw query finds nothing, each query
    token is snapped to its closest corpus token and retried. Same repo-scope
    rule + hit shape as :func:`recall`, so the two fuse cleanly. Empty on any
    failure / no FTS5. ``AIFORGE_MEM_SPELL=0`` disables correction."""
    toks = _kw_tokens(query)
    if not toks:
        return []
    with _conn() as c:
        # backfill FTS for rows written before the index existed (triggers only
        # cover new writes).
        try:
            fts_n = c.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
            u_n = c.execute("SELECT count(*) FROM memory_units").fetchone()[0]
            if u_n and fts_n < u_n:
                c.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
                c.commit()
        except sqlite3.OperationalError:
            return []                                    # no FTS5 → vector-only
        rows = _fts_rows(c, toks, repo, limit)
        corrected = False
        if not rows and os.environ.get("AIFORGE_MEM_SPELL", "1") != "0":
            fixed = _spell_correct(c, toks)
            if fixed != toks:
                rows = _fts_rows(c, fixed, repo, limit)
                corrected = True
    out: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        if r["text"] in seen:
            continue
        seen.add(r["text"])
        out.append({
            "text": r["text"], "title": r["title"],
            "source": r["source"] or "keyword", "group": f"kw:{r['id']}",
            "kind": r["kind"], "ticket": r["ticket"], "repo": r["repo"],
            # rank→[0,1] (BM25 order already applied); slight penalty if corrected
            "score": (1.0 - i / max(1, len(rows))) * (0.9 if corrected else 1.0),
        })
    return out


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


def _vec_recall(text, qvec, repo, limit: int, boost: set) -> list[dict]:
    """sqlite-vec KNN recall — semantic nearest-neighbours over the vec0 index.
    Over-fetches, then repo-scopes + applies the tag boost + dedups, matching
    :func:`recall`'s hit shape. Raises if the extension isn't loadable (no
    silent cosine fallback)."""
    # KNN over-fetch, then repo-scope in the join below. Over-fetch MUCH larger
    # when a repo is given: the top neighbours may be mostly OTHER repos, and the
    # repo filter would otherwise shrink the result under `limit`.
    k = max(limit * 20, 200) if repo else max(limit * 6, 48)
    with _conn() as c:
        rows = c.execute(
            "SELECT rowid AS id, distance FROM vec_memory "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (json.dumps(qvec), k)).fetchall()
        if not rows:
            return []
        dist = {r["id"]: float(r["distance"]) for r in rows}
        ids = list(dist)
        ph = ",".join("?" * len(ids))
        where = f"id IN ({ph})"
        params: list = list(ids)
        if repo:
            where += " AND (repo = ? OR repo IS NULL OR repo = 'shared')"
            params.append(repo)
        urows = c.execute(
            f"SELECT * FROM memory_units WHERE {where}", params).fetchall()
    scored: list[dict] = []
    for r in urows:
        # cosine distance in [0,2] → similarity in [0,1]
        score = max(0.0, 1.0 - dist.get(r["id"], 2.0))
        if boost:
            try:
                row_tags = {str(t).lower() for t in json.loads(r["tags"] or "[]")}
            except (TypeError, ValueError):
                row_tags = set()
            if row_tags & boost:
                score = min(1.0, score + 0.3)
        scored.append({
            "text": r["text"], "title": r["title"],
            "source": r["source"] or "memory", "group": f"sqlite:{r['id']}",
            "kind": r["kind"], "ticket": r["ticket"], "repo": r["repo"],
            "score": score})
    scored.sort(key=lambda h: -h["score"])
    seen: set = set()
    out: list[dict] = []
    for h in scored:
        if h["text"] in seen:
            continue
        seen.add(h["text"])
        out.append(h)
        if len(out) >= limit:
            break
    return out


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
    # Semantic backend → sqlite-vec KNN (real nearest-neighbour, no O(N) scan).
    # No cosine fallback here: a missing extension raises (loud) as the user
    # requires; the brute-force path below is only the dev/test 'hash' backend.
    if _vec_enabled():
        return _vec_recall(text, qvec, repo, limit, boost)
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


def recent(*, limit: int = 5, repo: str | None = None,
           exclude_kind: str | None = None) -> list[dict]:
    """The most-recently-written memory units (hot cache) — newest first, by
    ``created_at``/``id``. A just-captured fact surfaces immediately, before the
    embedding index or the next compaction folds it into a brief. ``repo`` filters
    to that repo + global/agnostic rows; ``exclude_kind`` drops a kind (e.g. the
    consolidated 'compacted'/'knowledge' briefs, so this returns raw fresh facts).
    Never raises."""
    if limit <= 0:
        return []
    where = []
    params: list = []
    if repo:
        where.append("(repo = ? OR repo IS NULL OR repo = 'shared')")
        params.append(repo)
    if exclude_kind:
        where.append("kind != ?")
        params.append(exclude_kind)
    sql = "SELECT * FROM memory_units"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _conn() as c:
            rows = c.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        txt = r["text"]
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append({
            "text": txt, "title": r["title"],
            "source": r["source"] or "recent",
            "group": f"recent:{r['id']}",
            "kind": r["kind"], "ticket": r["ticket"], "repo": r["repo"],
            # descending score preserves recency order through normalization
            "score": 1.0 - (i / max(1, len(rows))),
        })
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
            c.execute("DELETE FROM memory_units WHERE id = ?", (i,))
        return len(ids)


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


def reembed_all() -> dict:
    """Recompute EVERY unit's embedding with the ACTIVE embedder. Needed after a
    backend/model switch or a migration that imported rows embedded by a
    DIFFERENT embedder (mixed dims → broken KNN). The vec index rebuilds
    automatically: _conn's _init_vec recreates vec_memory at the active dim
    (dropping a stale-dim one) and the per-row UPDATE triggers repopulate it.
    Idempotent. Returns the count re-embedded."""
    n = 0
    with _LOCK, _conn() as c:      # _init_vec (in _conn) already fixed the vec dim
        rows = c.execute("SELECT id, text FROM memory_units").fetchall()
        for r in rows:
            v = local_embed.embed(r["text"] or "")
            c.execute("UPDATE memory_units SET embedding = ? WHERE id = ?",
                      (json.dumps(v), r["id"]))
            n += 1
    return {"reembedded": n}


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
