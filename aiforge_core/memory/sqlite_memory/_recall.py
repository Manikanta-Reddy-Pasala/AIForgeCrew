"""Semantic recall (sqlite-vec KNN / brute-force cosine) + recency cache."""
from __future__ import annotations

import json

from aiforge_core.memory import local_embed

from ._schema import _conn, _vec_enabled


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
