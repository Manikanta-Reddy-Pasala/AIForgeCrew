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
    """Call :8765 rerank sidecar, return top-`keep`."""
    if not hits:
        return []
    body = {
        "query": query,
        "candidates": [{"id": h.id, "text": h.text} for h in hits],
    }
    req = urllib.request.Request(
        f"{RERANK_URL}/rerank",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read().decode())
    order = resp["order"]
    scores = resp["scores"]
    out = []
    for pos in order[:keep]:
        h = hits[pos]
        h.score = float(scores[pos])
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
}


def retrieve_for_role(
    store,
    role: str,
    query: str,
    parent_id: str | None,
) -> list[Hit]:
    """Full pipeline per role: BM25 + vector per tier → RRF → rerank."""
    if role not in ROLE_POLICIES:
        raise KeyError(f"no retrieval policy for role {role}")
    policy = ROLE_POLICIES[role]
    rankings_bm25: list[list[Hit]] = []
    rankings_vec: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix = spec.get("wing_prefix")
        # Fact Extract scoped to one ticket
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"
        rankings_bm25.append(store.search_tier_bm25(
            tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
        rankings_vec.append(store.search_tier_vec(
            tier=tier, query=query, top_k=top_k, wing_prefix=wing_prefix))
    fused = rrf_fuse(rankings_bm25 + rankings_vec, k=60,
                     top_n=sum(s["top_k"] for s in policy["tiers"]))
    return rerank_http(query, fused, keep=policy["rerank_keep"])
