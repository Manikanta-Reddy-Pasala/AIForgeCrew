"""Write path — upsert Decision / Observation / Note / Doc nodes."""
from __future__ import annotations

import time

from ._entities import extract_entities
from ._helpers import (
    _SCHEMA_VERSION,
    _clamp01,
    _link_refs,
    _new_id,
    _text_hash,
)


# ─── Decision ─────────────────────────────────────────────────────────

_UPSERT_DECISION = """
MERGE (d:Decision_v2 {id: $id})
ON CREATE SET d.created_at     = datetime({epochSeconds: toInteger($now)}),
              d.schema_version = $schema_version
SET d.repo        = $repo,
    d.title       = $title,
    d.body        = $body,
    d.rationale   = $rationale,
    d.status      = $status,
    d.author      = $author,
    d.session_id  = $session_id,
    d.tags        = $tags,
    d.tags_text   = $tags_text,
    d.confidence  = $confidence,
    d.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH d
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(d)
RETURN d.id AS id
"""


def upsert_decision(
    driver,
    *,
    repo: str,
    title: str,
    body: str = "",
    rationale: str = "",
    status: str = "active",          # active | superseded | rejected
    author: str = "",
    session_id: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    supersedes_id: str | None = None,
    confidence: float = 1.0,
    id: str | None = None,
) -> dict:
    """Record a durable architectural / process decision."""
    nid = id or _new_id("dec")
    tags = list(tags or [])
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "rationale": rationale, "status": status, "author": author,
        "session_id": session_id, "tags": tags,
        "tags_text": " ".join(tags),
        "confidence": _clamp01(confidence),
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_DECISION, **params).consume()
        _link_refs(s, repo=repo, src_label="Decision_v2", src_id=nid,
                   refs=refs or [])
        if supersedes_id:
            s.run(
                "MATCH (a:Decision_v2 {id:$a}), (b:Decision_v2 {id:$b}) "
                "MERGE (a)-[:SUPERSEDES]->(b) "
                "SET b.status = 'superseded', "
                "    b.updated_at = datetime({epochSeconds: toInteger($now)})",
                a=nid, b=supersedes_id, now=time.time(),
            ).consume()
    return {"id": nid, "label": "Decision_v2"}


# ─── Observation ──────────────────────────────────────────────────────

_UPSERT_OBSERVATION = """
MERGE (o:Observation_v2 {id: $id})
ON CREATE SET o.created_at     = datetime({epochSeconds: toInteger($now)}),
              o.schema_version = $schema_version,
              o.seen_count     = 1
SET o.repo        = $repo,
    o.kind        = $kind,
    o.text        = $text,
    o.text_hash   = $text_hash,
    o.author      = $author,
    o.session_id  = $session_id,
    o.tags        = $tags,
    o.tags_text   = $tags_text,
    o.embed_vec   = $embed_vec,
    o.embed_model = $embed_model,
    o.media_refs  = $media_refs,
    o.confidence  = $confidence,
    o.importance  = $importance,
    o.entities    = $entities,
    o.event_time  = CASE
        WHEN $event_time IS NULL
        THEN datetime({epochSeconds: toInteger($now)})
        ELSE datetime({epochSeconds: toInteger($event_time)})
    END,
    o.valid_at    = CASE
        WHEN $valid_at IS NULL
        THEN datetime({epochSeconds: toInteger($now)})
        ELSE datetime({epochSeconds: toInteger($valid_at)})
    END,
    o.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH o
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(o)
RETURN o.id AS id
"""


# Exact-text dedupe lookup. Returns the existing Observation_v2 id when
# the same repo already holds a node with identical text. We don't key
# on tags/kind so a Learner that re-emits the same fact with a slightly
# different tag set still collapses — losing tag drift is acceptable;
# 3000 duplicate "README.md had 3 occurrences …" rows are not.
#
# Keyed on text_hash (indexed, see core/neo4j.py) so the lookup is an
# index seek instead of an O(n) label scan over long text properties;
# the WHERE re-verifies full text equality against hash collisions.
# Nodes written before the text_hash migration carry no hash and won't
# match — they pick up at most one fresh duplicate (which then becomes
# the dedupe target); decay archives the cold legacy copy.
_FIND_DUP_OBSERVATION = """
MATCH (o:Observation_v2 {repo: $repo, text_hash: $text_hash})
WHERE o.text = $text
RETURN o.id AS id, coalesce(o.seen_count, 1) AS seen_count
ORDER BY o.created_at ASC
LIMIT 1
"""


_TOUCH_DUP_OBSERVATION = """
MATCH (o:Observation_v2 {id: $id})
SET o.seen_count   = coalesce(o.seen_count, 1) + 1,
    o.last_seen_at = datetime({epochSeconds: toInteger($now)}),
    o.tags         = apoc.coll.toSet(coalesce(o.tags, []) + $tags),
    o.tags_text    = apoc.text.join(apoc.coll.toSet(coalesce(o.tags, []) + $tags), ' ')
RETURN o.id AS id
"""


# Same lookup minus the APOC merge (APOC isn't installed on every Neo4j
# Community deploy). We fall back to a plain timestamp+counter bump.
_TOUCH_DUP_OBSERVATION_NO_APOC = """
MATCH (o:Observation_v2 {id: $id})
SET o.seen_count   = coalesce(o.seen_count, 1) + 1,
    o.last_seen_at = datetime({epochSeconds: toInteger($now)})
RETURN o.id AS id
"""


def upsert_observation(
    driver,
    *,
    repo: str,
    text: str,
    kind: str = "note",              # note | bug | learning | gotcha | feedback
    author: str = "",
    session_id: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    embed_vec: list[float] | None = None,
    embed_model: str = "bge-m3",
    media_refs: list[str] | None = None,
    event_time: float | None = None,
    valid_at: float | None = None,
    confidence: float = 1.0,
    importance: float = 0.5,
    id: str | None = None,
    dedupe: bool = True,
    supersedes: list[str] | None = None,
) -> dict:
    """Record an agent / human observation.

    When ``dedupe=True`` (default) and an existing Observation_v2 with
    the same ``repo`` + ``text`` already exists, this returns the
    existing node's id and bumps ``seen_count`` + ``last_seen_at``
    instead of creating a duplicate. Pass ``dedupe=False`` to force a
    new node (e.g. tests, or when the caller has already done its own
    dedupe step).

    Embed vector is optional — when supplied, vector recall over
    Observation_v2 becomes available.

    ``media_refs`` (gap-10): list of image / video / file paths or URLs
    associated with the fact. Stored as a string array so a future
    vision-embed pipeline can pick them up; today they just round-trip
    so search results can surface "the fact mentioned screenshot X".

    ``event_time`` (gap-7, bi-temporal): epoch seconds for the
    real-world time the fact refers to, distinct from ``created_at``
    (the ingest timestamp). Defaults to the ingest moment when not
    supplied so old callers stay correct.

    ``supersedes`` (gap #2, contradiction resolution): ids of older
    Observation_v2 nodes this fact replaces. Each is marked
    ``status='superseded'`` with a ``SUPERSEDES`` edge from the new
    node, so the stale fact drops out of vector recall + the PPR
    reranker instead of co-existing with its correction.
    """
    tags = list(tags or [])
    text = (text or "").strip()
    media_refs = list(media_refs or [])

    if dedupe and text:
        with driver.session() as s:
            existing = s.run(
                _FIND_DUP_OBSERVATION, repo=repo, text=text,
                text_hash=_text_hash(text),
            ).single()
            if existing is not None:
                dup_id = existing["id"]
                try:
                    s.run(
                        _TOUCH_DUP_OBSERVATION,
                        id=dup_id, tags=tags, now=time.time(),
                    ).consume()
                except Exception:
                    # APOC not loaded — fall back to plain touch.
                    s.run(
                        _TOUCH_DUP_OBSERVATION_NO_APOC,
                        id=dup_id, now=time.time(),
                    ).consume()
                _link_refs(s, repo=repo, src_label="Observation_v2",
                           src_id=dup_id, refs=refs or [])
                return {
                    "id": dup_id, "label": "Observation_v2",
                    "deduped": True,
                    "seen_count": (existing["seen_count"] or 1) + 1,
                }

    nid = id or _new_id("obs")
    entities = [e["value"] for e in extract_entities(text)]
    params = {
        "id": nid, "repo": repo, "kind": kind, "text": text,
        "text_hash": _text_hash(text),
        "author": author, "session_id": session_id, "tags": tags,
        "tags_text": " ".join(tags),
        "embed_vec": embed_vec, "embed_model": embed_model,
        "media_refs": media_refs,
        "confidence": _clamp01(confidence),
        "importance": _clamp01(importance),
        "entities": entities,
        "event_time": event_time,
        # valid_at (bi-temporal): when the fact STARTED being true. Defaults
        # to event_time (real-world work time) so a fact about past work is
        # valid from then, not from ingest. invalid_at stays unset (= still
        # valid) until a supersede/invalidate time-bounds it.
        "valid_at": valid_at if valid_at is not None else event_time,
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_OBSERVATION, **params).consume()
        _link_refs(s, repo=repo, src_label="Observation_v2", src_id=nid,
                   refs=refs or [])
        for old_id in supersedes or []:
            old_id = (old_id or "").strip()
            if not old_id or old_id == nid:
                continue
            s.run(_SUPERSEDE_OBSERVATION,
                  a=nid, b=old_id, repo=repo, now=time.time()).consume()
    return {"id": nid, "label": "Observation_v2", "deduped": False,
            "superseded": [s for s in (supersedes or []) if s]}


# ─── Note ─────────────────────────────────────────────────────────────

_UPSERT_NOTE = """
MERGE (n:Note_v2 {id: $id})
ON CREATE SET n.created_at     = datetime({epochSeconds: toInteger($now)}),
              n.schema_version = $schema_version
SET n.repo        = $repo,
    n.title       = $title,
    n.body        = $body,
    n.author      = $author,
    n.tags        = $tags,
    n.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH n
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(n)
RETURN n.id AS id
"""


def upsert_note(
    driver,
    *,
    repo: str,
    title: str,
    body: str = "",
    author: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    id: str | None = None,
) -> dict:
    nid = id or _new_id("note")
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "author": author, "tags": list(tags or []),
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_NOTE, **params).consume()
        _link_refs(s, repo=repo, src_label="Note_v2", src_id=nid,
                   refs=refs or [])
    return {"id": nid, "label": "Note_v2"}


# ─── Doc (web doc / external) ─────────────────────────────────────────

_UPSERT_DOC = """
MERGE (d:Doc_v2 {id: $id})
ON CREATE SET d.created_at     = datetime({epochSeconds: toInteger($now)}),
              d.schema_version = $schema_version
SET d.repo        = $repo,
    d.url         = $url,
    d.title       = $title,
    d.body        = $body,
    d.source_kind = $source_kind,
    d.fetched_at  = datetime({epochSeconds: toInteger($now)})
WITH d
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(d)
RETURN d.id AS id
"""


def upsert_doc(
    driver,
    *,
    repo: str,
    title: str,
    body: str,
    url: str = "",
    source_kind: str = "web",       # web | readme | runbook | api-spec
    refs: list[str] | None = None,
    id: str | None = None,
) -> dict:
    nid = id or _new_id("doc")
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "url": url, "source_kind": source_kind,
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_DOC, **params).consume()
        _link_refs(s, repo=repo, src_label="Doc_v2", src_id=nid,
                   refs=refs or [])
    return {"id": nid, "label": "Doc_v2"}


# Gap #2: mark an older Observation superseded by a newer one and draw
# a SUPERSEDES edge (mirrors the Decision_v2 path). Superseded nodes are
# excluded from both vanilla recall (above) and the PPR reranker, so a
# corrected fact stops resurfacing alongside the stale one it replaced.
_SUPERSEDE_OBSERVATION = """
MATCH (a:Observation_v2 {id:$a}), (b:Observation_v2 {id:$b, repo:$repo})
MERGE (a)-[:SUPERSEDES]->(b)
SET b.status = 'superseded',
    b.superseded_by = $a,
    b.invalid_at = datetime({epochSeconds: toInteger($now)}),
    b.updated_at = datetime({epochSeconds: toInteger($now)})
"""
