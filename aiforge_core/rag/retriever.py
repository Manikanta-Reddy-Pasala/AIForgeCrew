"""Hybrid retrieval — BM25 + vector → RRF → rerank.

Uses store_v2's tier-aware search directly (works against our actual
`memories` table). LlamaIndex's PGVectorStore hardcodes `data_` prefix
which broke against our schema; bypassing it here keeps things simple.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from aiforge_core.retrieval import Hit, ROLE_POLICIES, rrf_fuse
from aiforge_core.store_v2 import Store


log = logging.getLogger("aiforge.rag.retriever")

RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")

_store: Store | None = None


def _get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def _vector_retrieve(store: Store, tier: str, wing_prefix: str | None,
                     query: str, top_k: int) -> list[Hit]:
    try:
        return store.search_tier_vec(tier, query, top_k, wing_prefix)
    except Exception as exc:
        log.warning("vector retrieve failed: %s", exc)
        return []


def _bm25_retrieve(store: Store, tier: str, wing_prefix: str | None,
                   query: str, top_k: int) -> list[Hit]:
    try:
        return store.search_tier_bm25(tier, query, top_k, wing_prefix)
    except Exception as exc:
        log.warning("bm25 retrieve failed: %s", exc)
        return []


def _rerank(query: str, hits: list[Hit], keep: int) -> list[Hit]:
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
        order: list[int] = resp["order"]
        scores: list[float] = resp["scores"]
        out: list[Hit] = []
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
    except Exception as exc:
        # Rerank sidecar retired 2026-04-23; RRF order is production ranking.
        log.debug("rerank skipped (%s); RRF order used", exc)
        return hits[:keep]


def retrieve_for_role_li(
    store: Any,
    role: str,
    query: str,
    parent_id: int | None,
) -> list[Hit]:
    """Hybrid BM25 + vector → RRF → rerank using store_v2 directly.

    The ``store`` arg is accepted for API compatibility but ignored —
    we instantiate a singleton Store() internally.
    """
    if not query or not query.strip():
        return []

    if role not in ROLE_POLICIES:
        log.warning("unknown role %r; using 'planner' policy", role)
        role = "planner"

    policy = ROLE_POLICIES[role]
    s = _get_store()

    rankings: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix: str | None = spec.get("wing_prefix")
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"

        vec_hits = _vector_retrieve(s, tier, wing_prefix, query, top_k)
        bm25_hits = _bm25_retrieve(s, tier, wing_prefix, query, top_k)
        rankings.extend([vec_hits, bm25_hits])

    if not any(rankings):
        log.warning("retrieve_for_role_li: all rankings empty (role=%s)", role)
        return []

    top_n = sum(s_["top_k"] for s_ in policy["tiers"])
    fused = rrf_fuse(rankings, k=60, top_n=top_n)
    return _rerank(query, fused, keep=policy["rerank_keep"])
