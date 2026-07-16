"""Recall — vector recall, semantic dedupe, PPR rerank, recency rerank."""
from __future__ import annotations

import math

from aiforge_memory.core.neo4j import vector_overfetch_k


_RECALL_OBSERVATION = """
CALL db.index.vector.queryNodes('codemem_observation_embed', $k_query, $vec)
YIELD node AS o, score
WHERE o.repo = $repo
  AND (o.status IS NULL OR o.status = 'active')
  AND (o.invalid_at IS NULL OR o.invalid_at > datetime())
RETURN o.id AS id, o.text AS text, o.kind AS kind,
       coalesce(o.tags,[]) AS tags, score,
       coalesce(o.importance, 0.5) AS importance,
       coalesce(o.confidence, 1.0) AS confidence,
       o.created_at.epochSeconds AS created_at_epoch
ORDER BY score DESC LIMIT $k
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
  AND (seed_node.invalid_at IS NULL OR seed_node.invalid_at > datetime())
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
  AND (cand.invalid_at IS NULL OR cand.invalid_at > datetime())
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
       overlap_norm AS overlap_score,
       coalesce(cand.importance, 0.5) AS importance,
       coalesce(cand.confidence, 1.0) AS confidence,
       cand.created_at.epochSeconds AS created_at_epoch
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
    w_importance: float = 0.15,
) -> list[dict]:
    """Re-rank recall ``rows`` blending raw relevance with recency,
    confidence and importance — a pure, driver-free post-processor callers
    apply on top of any recall (``recall_observations`` /
    ``recall_observations_ppr``).

    Each row is expected to carry ``score`` (relevance) and optionally
    ``created_at_epoch`` (epoch seconds), ``confidence`` (0..1), and
    ``importance`` (0..1, salience). The new score is::

        final = score
              + w_recency    * exp(-age_days / half_life_days)
              + w_conf       * (confidence - 1)
              + w_importance * (importance - 0.5)

    so fresher facts get a positive recency bump, low-confidence facts a
    (negative) penalty vs a fully-trusted (conf=1) fact, and high-salience
    facts (importance > 0.5) get a boost while low-salience ones (< 0.5)
    are pushed down — both relative to the neutral 0.5 default. Rows
    missing ``created_at_epoch`` get no recency bonus; missing
    ``confidence``/``importance`` default to 1.0 / 0.5 (no effect).
    Returns a new list of the same dicts (each gains a ``final_score``
    key) sorted descending."""
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
        imp = r.get("importance")
        imp = 0.5 if imp is None else float(imp)
        final = (score + w_recency * recency + w_conf * (conf - 1.0)
                 + w_importance * (imp - 0.5))
        r["final_score"] = final
        out.append(r)
    out.sort(key=lambda d: d["final_score"], reverse=True)
    return out
