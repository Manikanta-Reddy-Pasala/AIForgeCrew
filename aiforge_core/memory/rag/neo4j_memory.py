"""Neo4j-backed memory store (Option A: all memory reads/writes go via Neo4j).

Drop-in replacement for the Postgres memory path. Exposes the same
``retrieve_for_role_li`` and ``retain_fact`` / ``write_t1`` semantics used
by planner/doer/learner, but stores tiered facts as ``(:Memory)`` nodes in
Neo4j with a 1024-d ``bge-m3`` embedding in the ``memory_embedding_vec``
native vector index.

Env:
    AIFORGE_NEO4J_URI       bolt://127.0.0.1:7687
    AIFORGE_NEO4J_USER      neo4j
    AIFORGE_NEO4J_PASSWORD  password
    AIFORGE_EMBED_URL       http://127.0.0.1:8764  (bge-m3 sidecar)
    AIFORGE_EMBED_DIM       1024

Schema (keyed by synthetic ``fact_id`` so concurrent writers don't clash):
    (:Memory {
        fact_id, tier, wing, kind, title, text,
        source, parent_id, embedding, metadata,
        created_at, expires_at
    })

Indexes (created on demand):
    CREATE CONSTRAINT memory_fact_id FOR (m:Memory) REQUIRE m.fact_id IS UNIQUE
    CREATE INDEX memory_tier_wing   FOR (m:Memory) ON (m.tier, m.wing)
    CREATE VECTOR INDEX memory_embedding_vec FOR (m:Memory) ON (m.embedding)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from aiforge_core.memory.retrieval import Hit, ROLE_POLICIES, rrf_fuse

log = logging.getLogger("aiforge.rag.neo4j_memory")

from aiforge_core.memory.neo4j_conn import neo4j_params

NEO4J_URI, NEO4J_USER, NEO4J_PASS = neo4j_params()
EMBED_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
EMBED_DIM = int(os.environ.get("AIFORGE_EMBED_DIM", "1024"))

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        from neo4j import GraphDatabase

        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    return _driver


def ensure_schema() -> None:
    """Idempotent. Run once at import or before first write."""
    stmts = [
        "CREATE CONSTRAINT memory_fact_id IF NOT EXISTS FOR (m:Memory) "
        "REQUIRE m.fact_id IS UNIQUE",
        "CREATE INDEX memory_tier_wing IF NOT EXISTS FOR (m:Memory) "
        "ON (m.tier, m.wing)",
        "CREATE FULLTEXT INDEX memory_text_ft IF NOT EXISTS FOR (m:Memory) "
        "ON EACH [m.title, m.text]",
        # vector index creation is dialect-specific; try both forms.
        f"CREATE VECTOR INDEX memory_embedding_vec IF NOT EXISTS "
        f"FOR (m:Memory) ON (m.embedding) OPTIONS {{indexConfig: {{"
        f"  `vector.dimensions`: {EMBED_DIM}, "
        f"  `vector.similarity_function`: 'cosine' "
        f"}}}}",
    ]
    with _get_driver().session() as s:
        for q in stmts:
            try:
                s.run(q)
            except Exception as exc:
                log.warning("schema init failed on %r: %s", q.split("\n")[0], exc)


# ─────────────── embeddings ───────────────

def _embed(text: str) -> list[float] | None:
    """Call bge-m3 sidecar. Shape: POST /embed {"text": "..."}
    → {"embedding": [...]}. Falls back to None on any failure."""
    if not text:
        return None
    try:
        req = urllib.request.Request(
            f"{EMBED_URL}/embed",
            data=json.dumps({"text": text[:8000]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        emb = resp.get("embedding") or resp.get("vector")
        return list(emb) if emb else None
    except Exception as exc:
        log.debug("embed failed (%s); storing without vector", exc)
        return None


# ─────────────── writes ───────────────

@dataclass
class MemoryRow:
    tier: str
    wing: str
    text: str
    kind: str = "fact"
    title: str | None = None
    source: str | None = None
    parent_id: str | None = None
    metadata: dict | None = None
    expires_at: str | None = None


def retain_fact(row: MemoryRow) -> str:
    """Insert/update one memory node. Returns fact_id."""
    fact_id = str(uuid.uuid4())
    embedding = _embed(f"{row.title or ''}\n{row.text}")
    params = {
        "fact_id": fact_id,
        "tier": row.tier,
        "wing": row.wing,
        "kind": row.kind,
        "title": row.title or "",
        "text": row.text,
        "source": row.source or "",
        "parent_id": row.parent_id or "",
        "embedding": embedding,
        "metadata": json.dumps(row.metadata or {}),
        "expires_at": row.expires_at,
    }
    with _get_driver().session() as s:
        s.run(
            "MERGE (m:Memory {fact_id: $fact_id}) "
            "SET m.tier=$tier, m.wing=$wing, m.kind=$kind, "
            "    m.title=$title, m.text=$text, m.source=$source, "
            "    m.parent_id=$parent_id, m.metadata=$metadata, "
            "    m.embedding=$embedding, m.created_at=timestamp(), "
            "    m.expires_at=$expires_at",
            **params,
        )
    return fact_id


def write_t1(ticket_id: str, text: str, title: str | None = None,
             metadata: dict | None = None) -> str:
    """Learner's one-shot T1 digest write."""
    return retain_fact(MemoryRow(
        tier="t1",
        wing=f"ticket/{ticket_id}",
        text=text,
        kind="digest",
        title=title,
        source="learner",
        parent_id=ticket_id,
        metadata=metadata,
    ))


# ─────────────── retrieve (mirror of aiforge_core.rag.retriever API) ───────────────

def _vector_hits(tier: str, wing_prefix: str | None,
                 query_vec: list[float], top_k: int) -> list[Hit]:
    if not query_vec:
        return []
    cypher = (
        "CALL db.index.vector.queryNodes('memory_embedding_vec', $k, $vec) "
        "YIELD node, score "
        "WHERE node.tier = $tier "
        "  AND ($wing_prefix = '' OR node.wing STARTS WITH $wing_prefix) "
        "RETURN node.fact_id AS fact_id, node.tier AS tier, node.wing AS wing, "
        "       node.title AS title, node.text AS text, score AS score"
    )
    out: list[Hit] = []
    try:
        with _get_driver().session() as s:
            for r in s.run(cypher, k=top_k * 3, vec=query_vec, tier=tier,
                           wing_prefix=wing_prefix or ""):
                out.append(Hit(
                    id=r["fact_id"], tier=r["tier"],
                    source=r["wing"],
                    title=r["title"] or "", text=r["text"] or "",
                    score=float(r["score"] or 0.0),
                ))
                if len(out) >= top_k:
                    break
    except Exception as exc:
        log.warning("neo4j vector retrieve failed: %s", exc)
    return out


def _bm25_hits(tier: str, wing_prefix: str | None,
               query: str, top_k: int) -> list[Hit]:
    cypher = (
        "CALL db.index.fulltext.queryNodes('memory_text_ft', $q) "
        "YIELD node, score "
        "WHERE node.tier = $tier "
        "  AND ($wing_prefix = '' OR node.wing STARTS WITH $wing_prefix) "
        "RETURN node.fact_id AS fact_id, node.tier AS tier, node.wing AS wing, "
        "       node.title AS title, node.text AS text, score AS score "
        "LIMIT $k"
    )
    out: list[Hit] = []
    try:
        with _get_driver().session() as s:
            for r in s.run(cypher, q=query, tier=tier, k=top_k,
                           wing_prefix=wing_prefix or ""):
                out.append(Hit(
                    id=r["fact_id"], tier=r["tier"],
                    source=r["wing"],
                    title=r["title"] or "", text=r["text"] or "",
                    score=float(r["score"] or 0.0),
                ))
    except Exception as exc:
        log.warning("neo4j bm25 retrieve failed: %s", exc)
    return out


def retrieve_for_role_li(
    store: Any,  # kept for API compat; ignored
    role: str,
    query: str,
    parent_id: str | int | None,
) -> list[Hit]:
    """Hybrid BM25 + vector retrieval against Neo4j ``(:Memory)`` nodes.

    Signature matches ``aiforge_core.rag.retriever.retrieve_for_role_li`` so
    callers swap transparently via the ``AIFORGE_MEMORY_BACKEND`` flag.
    """
    if not query or not query.strip():
        return []

    if role not in ROLE_POLICIES:
        role = "planner"
    policy = ROLE_POLICIES[role]

    qvec = _embed(query)

    rankings: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix: str | None = spec.get("wing_prefix")
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"
        rankings.append(_vector_hits(tier, wing_prefix, qvec or [], top_k))
        rankings.append(_bm25_hits(tier, wing_prefix, query, top_k))

    if not any(rankings):
        log.debug("retrieve neo4j memory: all rankings empty (role=%s)", role)
        return []

    top_n = sum(spec["top_k"] for spec in policy["tiers"])
    fused = rrf_fuse(rankings, k=60, top_n=top_n)
    # RRF is our final ranking; rerank sidecar retired.
    keep = policy.get("rerank_keep", top_n)
    return fused[:keep]
