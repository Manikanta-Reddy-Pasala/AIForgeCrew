"""Keyword / BM25 (FTS5) recall with spell correction."""
from __future__ import annotations

import os
import re as _re
import sqlite3

from ._schema import _conn, _db_path

_STOP = {"the", "and", "for", "with", "that", "this", "how", "why", "what",
         "are", "was", "our", "you", "your", "from", "into", "not"}
_VOCAB_CACHE: dict = {}


def _kw_tokens(q: str) -> list[str]:
    toks = [t for t in _re.findall(r"[a-z0-9][a-z0-9_-]+", (q or "").lower())
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
