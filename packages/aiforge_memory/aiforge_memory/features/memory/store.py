"""Cypher writer for the memory layer — Decision_v2, Observation_v2,
Note_v2, Doc_v2 + MENTIONS / SUPERSEDES / RECORDS edges.

Public surface:
    upsert_decision(driver, *, repo, **fields)        -> dict
    upsert_observation(driver, *, repo, **fields)     -> dict
    upsert_note(driver, *, repo, **fields)            -> dict
    upsert_doc(driver, *, repo, **fields)             -> dict
    forget(driver, *, repo, node_id, label)           -> dict
    list_memory(driver, *, repo, label=None, limit=50) -> list[dict]
    recall_observations(driver, *, repo, query_vec, k=10) -> list[dict]

Idempotent: keyed on caller-supplied id (uuid4 if omitted). Auto-stamps
created_at, updated_at, schema_version. References (refs=[fqname|path])
become MENTIONS edges to Symbol_v2 / File_v2 nodes if they exist.

Memory nodes are *additive* — they coexist with the code graph and can
be retrieved either directly (by id) or via vector recall over
Observation embeddings.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from collections.abc import Iterable

from aiforge_memory.core.neo4j import vector_overfetch_k

_SCHEMA_VERSION = "codemem-v1"

_ALLOWED_LABELS = {"Decision_v2", "Observation_v2", "Note_v2", "Doc_v2"}


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
    o.entities    = $entities,
    o.event_time  = CASE
        WHEN $event_time IS NULL
        THEN datetime({epochSeconds: toInteger($now)})
        ELSE datetime({epochSeconds: toInteger($event_time)})
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
    confidence: float = 1.0,
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
        "entities": entities,
        "event_time": event_time,
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


# ─── Maintenance ──────────────────────────────────────────────────────

def forget(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Hard-delete a memory node + its edges. ``label`` must be one of
    Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "WITH n, n.id AS id DETACH DELETE n RETURN id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo).single()
    return {"deleted": row["id"] if row else None}


def soft_forget(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Soft-delete a memory node by flagging ``status='deleted'`` +
    stamping ``deleted_at``, instead of the hard ``DETACH DELETE`` that
    ``forget`` performs. The node + edges stay intact so it can be
    ``restore``d, and vanilla recall (which only includes
    ``status IS NULL OR status = 'active'``) drops it. ``label`` must be
    one of Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "SET n.status = 'deleted', "
        "    n.deleted_at = datetime({epochSeconds: toInteger($now)}), "
        "    n.updated_at = datetime({epochSeconds: toInteger($now)}) "
        "RETURN n.id AS id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo, now=time.time()).single()
    return {"soft_deleted": row["id"] if row else None}


def restore(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Reverse a ``soft_forget`` — set ``status='active'`` and clear
    ``deleted_at`` so the node re-enters recall. ``label`` must be one of
    Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "SET n.status = 'active', "
        "    n.deleted_at = null, "
        "    n.updated_at = datetime({epochSeconds: toInteger($now)}) "
        "RETURN n.id AS id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo, now=time.time()).single()
    return {"restored": row["id"] if row else None}


def list_memory(
    driver, *, repo: str, label: str | None = None, limit: int = 50,
) -> list[dict]:
    """Return memory nodes for a repo, newest first."""
    if label and label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    if label:
        cy = (
            f"MATCH (n:{label} {{repo:$repo}}) "
            "RETURN n.id AS id, labels(n)[0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    else:
        cy = (
            "MATCH (r:Repo {name:$repo})-[:RECORDS]->(n) "
            "WHERE any(l IN labels(n) WHERE l IN "
            "  ['Decision_v2','Observation_v2','Note_v2','Doc_v2']) "
            "RETURN n.id AS id, [l IN labels(n) WHERE l ENDS WITH '_v2'][0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    with driver.session() as s:
        return [dict(r) for r in s.run(cy, repo=repo, limit=limit)]


_RECALL_OBSERVATION = """
CALL db.index.vector.queryNodes('codemem_observation_embed', $k_query, $vec)
YIELD node AS o, score
WHERE o.repo = $repo
  AND (o.status IS NULL OR o.status = 'active')
RETURN o.id AS id, o.text AS text, o.kind AS kind,
       coalesce(o.tags,[]) AS tags, score
ORDER BY score DESC LIMIT $k
"""


# Gap #2: mark an older Observation superseded by a newer one and draw
# a SUPERSEDES edge (mirrors the Decision_v2 path). Superseded nodes are
# excluded from both vanilla recall (above) and the PPR reranker, so a
# corrected fact stops resurfacing alongside the stale one it replaced.
_SUPERSEDE_OBSERVATION = """
MATCH (a:Observation_v2 {id:$a}), (b:Observation_v2 {id:$b, repo:$repo})
MERGE (a)-[:SUPERSEDES]->(b)
SET b.status = 'superseded',
    b.superseded_by = $a,
    b.updated_at = datetime({epochSeconds: toInteger($now)})
"""


def recall_observations(
    driver, *, repo: str, query_vec: list[float], k: int = 10,
) -> list[dict]:
    if not query_vec:
        return []
    with driver.session() as s:
        return [dict(r) for r in s.run(
            _RECALL_OBSERVATION, repo=repo, vec=query_vec,
            k=k, k_query=vector_overfetch_k(k),
        )]


def find_semantic_dup(
    driver, *, repo: str, embed_vec: list[float] | None,
    threshold: float = 0.92,
) -> str | None:
    """Vector-recall the single nearest Observation_v2 in ``repo`` and
    return its id when the cosine score ``>= threshold``, else ``None``.

    This lifts semantic dedupe into the core store so any caller (not
    just the AIForgeCrew Learner) can collapse paraphrases before
    writing a near-identical fact. Uses the same vector index path as
    :func:`recall_observations` (``_RECALL_OBSERVATION``). Returns
    ``None`` when no embed vector is supplied — never touches the
    driver in that case."""
    if not embed_vec:
        return None
    with driver.session() as s:
        row = s.run(
            _RECALL_OBSERVATION, repo=repo, vec=embed_vec,
            k=1, k_query=vector_overfetch_k(1),
        ).single()
    if row is None:
        return None
    score = row["score"]
    if score is not None and score >= threshold:
        return row["id"]
    return None


# Gap-6 (PPR-lite): personalized-PageRank-style rerank without
# requiring the GDS plugin. Vector recall picks seeds; we then expand
# 1-hop via :MENTIONS to neighbouring Files/Symbols and lift any
# *other* Observation that points at the same neighbours, weighted by
# overlap. Final score = ``$alpha * vector_score + (1-$alpha) *
# overlap_score``, normalized to [0..1].
#
# Real PPR runs many damped iterations; this is 1 iteration with a
# fixed teleport mass on the seed set. Adequate for "find observations
# topologically near my seed" without dragging GDS in. Index migration
# can swap to ``gds.pageRank.stream`` later — same return contract.
_RECALL_OBSERVATIONS_PPR = """
// 1. Vector recall over Observation_v2 → seed set with score.
CALL db.index.vector.queryNodes('codemem_observation_embed', $seed_k_query, $vec)
YIELD node AS seed_node, score AS vec_score
WHERE seed_node.repo = $repo
  AND (seed_node.status IS NULL OR seed_node.status = 'active')
WITH collect({obs_id: seed_node.id, vec: vec_score}) AS seeds

// 2. Re-fetch seeds as node bindings + carry vector score.
UNWIND seeds AS s
MATCH (seed:Observation_v2 {id: s.obs_id, repo: $repo})
WITH seeds, seed, s.vec AS seed_vec

// 3. 1-hop neighbours of every seed via :MENTIONS.
OPTIONAL MATCH (seed)-[:MENTIONS]->(nbr)
WHERE nbr:File_v2 OR nbr:Symbol_v2
WITH seeds, collect(DISTINCT nbr) AS neighbour_set

// 4. Find every Observation_v2 in the same repo that mentions at
//    least one of those neighbours; count overlap per candidate.
UNWIND neighbour_set AS n
MATCH (cand:Observation_v2 {repo: $repo})-[:MENTIONS]->(n)
WHERE (cand.status IS NULL OR cand.status = 'active')
WITH seeds, cand, count(DISTINCT n) AS overlap

// 5. Aggregate one row per candidate so we can normalize overlap.
WITH seeds, cand, max(overlap) AS overlap

// 6. Compute max_overlap across the candidate set so we can scale
//    overlap into [0..1].
WITH seeds, collect({cand: cand, overlap: overlap}) AS rows
WITH seeds, rows,
     reduce(m = 0, r IN rows |
        CASE WHEN r.overlap > m THEN r.overlap ELSE m END) AS max_overlap

// 7. Also gather every seed even if it had no MENTIONS neighbour,
//    so direct vector hits without neighbours still show up.
UNWIND seeds AS s
OPTIONAL MATCH (seed_node:Observation_v2 {id: s.obs_id, repo: $repo})
WITH rows, max_overlap, s.obs_id AS sid, s.vec AS sv, seed_node
WITH rows, max_overlap,
     collect({cand: seed_node, overlap: 0,
              is_seed: true, vec: sv}) AS seed_rows
WITH rows + seed_rows AS merged, max_overlap

// 8. Score each candidate, picking the highest vector score across
//    duplicates so seed appearances win over neighbour-only rows.
UNWIND merged AS m
WITH m.cand AS cand, m.overlap AS overlap, max_overlap,
     coalesce(m.vec, 0.0) AS vec
WHERE cand IS NOT NULL
WITH cand,
     max(vec) AS direct_vec,
     max(overlap) AS overlap,
     max_overlap
WITH cand, direct_vec,
     CASE WHEN max_overlap = 0 THEN 0.0
          ELSE toFloat(overlap) / toFloat(max_overlap) END AS overlap_norm
WITH cand,
     ($alpha * direct_vec) +
     ((1.0 - $alpha) * overlap_norm) AS ppr_score,
     direct_vec, overlap_norm

RETURN cand.id AS id,
       cand.text AS text,
       cand.kind AS kind,
       coalesce(cand.tags, []) AS tags,
       ppr_score AS score,
       direct_vec AS vec_score,
       overlap_norm AS overlap_score
ORDER BY ppr_score DESC
LIMIT $k
"""


def recall_observations_ppr(
    driver, *, repo: str, query_vec: list[float],
    k: int = 10, seed_k: int = 25, alpha: float = 0.6,
) -> list[dict]:
    """Vector recall + 1-iteration personalized-PageRank rerank.

    Args:
        repo: scope all reads to one repo.
        query_vec: 1024-d bge-m3 vector.
        k: number of results to return.
        seed_k: vector-recall fan-in before graph rerank.
        alpha: ``score = alpha * vec_score + (1 - alpha) *
            overlap_score``. ``alpha=1.0`` collapses to vanilla
            vector recall; ``alpha=0.0`` ranks purely by neighbour
            overlap (rarely what you want).

    Returns a list of ``{id, text, kind, tags, score, vec_score,
    overlap_score}`` dicts ordered by descending blended score.
    Empty ``query_vec`` returns ``[]``.

    See :sql:`_RECALL_OBSERVATIONS_PPR` for the Cypher implementation.
    """
    if not query_vec:
        return []
    with driver.session() as s:
        return [dict(r) for r in s.run(
            _RECALL_OBSERVATIONS_PPR,
            repo=repo, vec=query_vec,
            seed_k_query=vector_overfetch_k(seed_k),
            k=k, alpha=float(alpha),
        )]


# ─── M1: recency / importance-weighted rerank (pure) ──────────────────

def rerank_by_recency(
    rows: list[dict],
    *,
    now: float,
    half_life_days: float = 30.0,
    w_recency: float = 0.2,
    w_conf: float = 0.1,
) -> list[dict]:
    """Re-rank recall ``rows`` blending raw relevance with recency and
    confidence — a pure, driver-free post-processor callers apply on top
    of any recall (``recall_observations`` / ``recall_observations_ppr``).

    Each row is expected to carry ``score`` (relevance) and optionally
    ``created_at_epoch`` (epoch seconds) + ``confidence`` (0..1). The new
    score is::

        final = score
              + w_recency * exp(-age_days / half_life_days)
              + w_conf    * (confidence - 1)

    so fresher facts get a positive recency bump and low-confidence facts
    get a (negative) penalty relative to a fully-trusted (conf=1) fact.
    Rows missing ``created_at_epoch`` get no recency bonus; rows missing
    ``confidence`` default to 1.0 (no penalty). Returns a new list of the
    same dicts (each gains a ``final_score`` key) sorted descending."""
    out: list[dict] = []
    half = half_life_days if half_life_days > 0 else 1.0
    for row in rows:
        r = dict(row)
        score = float(r.get("score") or 0.0)
        created = r.get("created_at_epoch")
        if created is not None:
            age_days = max(0.0, (now - float(created)) / 86_400.0)
            recency = math.exp(-age_days / half)
        else:
            recency = 0.0
        conf = r.get("confidence")
        conf = 1.0 if conf is None else float(conf)
        final = score + w_recency * recency + w_conf * (conf - 1.0)
        r["final_score"] = final
        out.append(r)
    out.sort(key=lambda d: d["final_score"], reverse=True)
    return out


# ─── M4: lightweight entity extraction (pure, regex KISS) ─────────────

# Code-file extensions we treat a bare token as a "file" for even when it
# has no slash (e.g. ``config.yaml``).
_CODE_EXTS = (
    "py", "java", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "c", "h",
    "cpp", "hpp", "cs", "kt", "scala", "swift", "php", "sql", "sh", "yaml",
    "yml", "json", "toml", "xml", "html", "css", "md", "txt", "cfg", "ini",
    "properties", "gradle", "lock", "env",
)

_RE_URL = re.compile(r"https?://[^\s<>\"')]+")
_RE_TICKET = re.compile(r"\b[A-Z]{2,}-\d+\b")
_RE_ENV = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_RE_FILE = re.compile(
    r"\b[\w./\\-]+/[\w./\\-]+"                       # contains a slash
    r"|\b[\w-]+\.(?:" + "|".join(_CODE_EXTS) + r")\b"  # or code ext
)
# fqname: A::b  or  pkg.func / Class.method (alnum dotted, 2+ parts).
_RE_SYMBOL = re.compile(
    r"\b[A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)+\b"       # foo::bar
    r"|\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b"      # foo.bar.baz
)


def extract_entities(text: str) -> list[dict]:
    """Pull structured entities out of free text with cheap regexes.

    Returns a deduped list of ``{"type": ..., "value": ...}`` where type
    is one of ``url``, ``file``, ``ticket``, ``env``, ``symbol``. KISS:
    no NLP, no model — just patterns. Order is stable (URLs first, then
    files, tickets, env vars, symbols) and exact-(type,value) duplicates
    are collapsed. Matched URLs/files are masked before symbol detection
    so ``example.com`` inside a URL isn't double-counted as a symbol."""
    text = text or ""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": kind, "value": value})

    masked = text
    for m in _RE_URL.finditer(text):
        _add("url", m.group(0))
    masked = _RE_URL.sub(" ", masked)
    for m in _RE_FILE.finditer(masked):
        _add("file", m.group(0))
    file_masked = _RE_FILE.sub(" ", masked)
    for m in _RE_TICKET.finditer(text):
        _add("ticket", m.group(0))
    for m in _RE_ENV.finditer(text):
        _add("env", m.group(0))
    # symbols last, over text with URLs + files stripped so we don't
    # re-flag dotted file/host tokens as symbols.
    for m in _RE_SYMBOL.finditer(file_masked):
        _add("symbol", m.group(0))
    return out


# ─── helpers ──────────────────────────────────────────────────────────

def _clamp01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 1.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _text_hash(text: str) -> str:
    """Short sha256 of the observation text — indexed dedupe key
    (full text equality is re-verified at lookup time)."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _link_refs(
    session, *, repo: str, src_label: str, src_id: str, refs: Iterable[str],
) -> None:
    """Create MENTIONS edges from the memory node to existing
    Symbol_v2 (matched by fqname) or File_v2 (matched by path).

    A ref string with `::` is treated as a symbol fqname; otherwise as a
    file path. Missing targets are silently ignored — no placeholders."""
    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if "::" in ref:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:Symbol_v2 {repo:$repo, fqname:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        else:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:File_v2 {repo:$repo, path:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        session.run(cy, sid=src_id, repo=repo, ref=ref).consume()
