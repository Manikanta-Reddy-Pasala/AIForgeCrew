"""Hybrid retrieval: per-tier BM25 + vector → RRF → rerank → pack.

All reads go through Store.search_tier_bm25 / search_tier_vec helpers.
Fusion is Reciprocal Rank Fusion (k=60 default).
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable


RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")


@dataclass
class Hit:
    id: str
    score: float
    source: str | None = None
    tier: str | None = None
    text: str = ""
    title: str | None = None
    metadata: dict = field(default_factory=dict)


def rrf_fuse(rankings: list[list[Hit]], k: int = 60, top_n: int = 30) -> list[Hit]:
    """Reciprocal Rank Fusion over multiple ranked lists."""
    agg: dict[str, Hit] = {}
    totals: dict[str, float] = {}
    for ranked in rankings:
        for rank, h in enumerate(ranked, start=1):
            totals[h.id] = totals.get(h.id, 0.0) + 1.0 / (k + rank)
            if h.id not in agg or len(agg[h.id].text) < len(h.text):
                agg[h.id] = h
    out = [Hit(
        id=hid, score=totals[hid],
        source=agg[hid].source, tier=agg[hid].tier,
        text=agg[hid].text, title=agg[hid].title, metadata=agg[hid].metadata,
    ) for hid in totals]
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:top_n]


def rerank_http(query: str, hits: list[Hit], keep: int) -> list[Hit]:
    """Call :8765 rerank sidecar, return top-`keep`. On sidecar failure,
    fall back to the fused (RRF) order so retrieval still works."""
    if not hits:
        return []
    body = {
        "query": query,
        "candidates": [{"id": h.id, "text": h.text} for h in hits],
    }
    try:
        req = urllib.request.Request(
            f"{RERANK_URL}/rerank",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode())
        order = resp["order"]
        scores = resp["scores"]
    except Exception as exc:
        # Sidecar unreachable, slow, or malformed. Downgrade to RRF order
        # rather than fail the caller.
        import logging
        logging.getLogger("aiforge.retrieval").warning(
            "rerank sidecar failed (%s); falling back to RRF order", exc,
        )
        return hits[:keep]
    out = []
    for pos in order[:keep]:
        if pos < 0 or pos >= len(hits):
            continue
        h = hits[pos]
        try:
            h.score = float(scores[pos])
        except (ValueError, IndexError):
            pass
        out.append(h)
    return out


ROLE_POLICIES: dict[str, dict] = {
    "architect": {
        "tiers": [
            {"tier": "t2", "top_k": 8},
            {"tier": "t4", "top_k": 8, "wing_prefix": "code/"},
            {"tier": "t3", "top_k": 4, "wing_prefix": "skills"},
            {"tier": "t1", "top_k": 8},
        ],
        "rerank_keep": 10,
    },
    "sr_developer": {
        "tiers": [
            {"tier": "t2", "top_k": 6},
            {"tier": "t3", "top_k": 8, "wing_prefix": "skills"},
            {"tier": "t4", "top_k": 12, "wing_prefix": "code/"},
            {"tier": "t1", "top_k": 8},
        ],
        "rerank_keep": 12,
    },
    "developer": {
        "tiers": [
            {"tier": "t4", "top_k": 20, "wing_prefix": "code/"},
            {"tier": "t3", "top_k": 6, "wing_prefix": "skills"},
            {"tier": "t1", "top_k": 8},
            {"tier": "t2", "top_k": 4},
        ],
        "rerank_keep": 15,
    },
    "fact_extract": {
        "tiers": [
            {"tier": "t1", "top_k": 200},
        ],
        "rerank_keep": 50,
    },
    # canonical role names
    "supervisor": {
        "tiers": [
            {"tier": "t1", "top_k": 20},
            {"tier": "t3", "top_k": 6, "wing_prefix": "decisions/"},
            {"tier": "t2", "top_k": 4},
        ],
        "rerank_keep": 10,
    },
    "planner": {
        "tiers": [
            {"tier": "t2", "top_k": 6},
            {"tier": "t3", "top_k": 8, "wing_prefix": "skills"},
            {"tier": "t4", "top_k": 12, "wing_prefix": "code/"},
            {"tier": "t1", "top_k": 8},
        ],
        "rerank_keep": 12,
    },
    "doer": {
        "tiers": [
            {"tier": "t4", "top_k": 20, "wing_prefix": "code/"},
            {"tier": "t3", "top_k": 6, "wing_prefix": "skills"},
            {"tier": "t1", "top_k": 8},
            {"tier": "t2", "top_k": 4},
        ],
        "rerank_keep": 15,
    },
    "feedback": {
        "tiers": [
            {"tier": "t4", "top_k": 10, "wing_prefix": "code/"},
            {"tier": "t1", "top_k": 10},
            {"tier": "t3", "top_k": 4, "wing_prefix": "skills"},
        ],
        "rerank_keep": 8,
    },
    "learner": {
        "tiers": [
            {"tier": "t1", "top_k": 50},
            {"tier": "t3", "top_k": 10},
        ],
        "rerank_keep": 20,
    },
}


def retrieve_for_role(
    store,
    role: str,
    query: str,
    parent_id: str | None,
) -> list[Hit]:
    """Full pipeline per role: BM25 + vector per tier → RRF → rerank.

    Resilience:
      - unknown role name → fallback to 'planner' policy (log warn).
      - per-tier BM25/vec errors → skip that ranking, don't fail run.
      - rerank sidecar failure → handled in rerank_http (RRF fallback).
    """
    import logging
    log = logging.getLogger("aiforge.retrieval")
    if not query or not query.strip():
        return []
    if role not in ROLE_POLICIES:
        log.warning("unknown role %r for retrieval; using 'planner' policy", role)
        role = "planner"
    policy = ROLE_POLICIES[role]
    rankings_bm25: list[list[Hit]] = []
    rankings_vec: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix = spec.get("wing_prefix")
        # Episodic wing scoped to one ticket if parent_id is given.
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"
        try:
            rankings_bm25.append(store.search_tier_bm25(
                tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
        except Exception as exc:
            log.warning("bm25 tier=%s wing_prefix=%s failed: %s",
                        tier, wing_prefix, exc)
        try:
            rankings_vec.append(store.search_tier_vec(
                tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
        except Exception as exc:
            log.warning("vec tier=%s wing_prefix=%s failed: %s",
                        tier, wing_prefix, exc)
    if not rankings_bm25 and not rankings_vec:
        log.warning("retrieve_for_role: all rankings empty (role=%s)", role)
        return []
    fused = rrf_fuse(rankings_bm25 + rankings_vec, k=60,
                     top_n=sum(s["top_k"] for s in policy["tiers"]))
    return rerank_http(query, fused, keep=policy["rerank_keep"])
