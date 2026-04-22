"""LlamaIndex hybrid retrieval — drop-in replacement for retrieval.retrieve_for_role.

Returns the same ``Hit`` dataclass so memory.py can convert hits to
``SearchResult`` objects unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import TYPE_CHECKING, Any

from aiforge_core.retrieval import Hit, ROLE_POLICIES, rrf_fuse

if TYPE_CHECKING:
    from llama_index.core import VectorStoreIndex


log = logging.getLogger("aiforge.rag.retriever")

RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")


def _node_to_hit(node_with_score: Any) -> Hit:
    node = node_with_score.node
    meta: dict[str, Any] = dict(node.metadata or {})
    return Hit(
        id=str(meta.get("id") or node.node_id),
        score=float(node_with_score.score or 0.0),
        source=meta.get("source"),
        tier=meta.get("tier"),
        text=node.get_content(),
        title=meta.get("title"),
        metadata=meta,
    )


def _vector_retrieve(index: Any, query: str, top_k: int) -> list[Hit]:
    from llama_index.core import QueryBundle
    from llama_index.core.retrievers import VectorIndexRetriever

    retriever = VectorIndexRetriever(index=index, similarity_top_k=top_k)
    try:
        nodes = retriever.retrieve(QueryBundle(query_str=query))
        return [_node_to_hit(n) for n in nodes]
    except Exception as exc:
        log.warning("vector retrieve failed: %s", exc)
        return []


def _bm25_retrieve(index: Any, query: str, top_k: int) -> list[Hit]:
    try:
        from llama_index.retrievers.bm25 import BM25Retriever

        bm25 = BM25Retriever.from_defaults(index=index, similarity_top_k=top_k)
        nodes = bm25.retrieve(query)
        return [_node_to_hit(n) for n in nodes]
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
        log.warning("rerank sidecar failed (%s); falling back to RRF order", exc)
        return hits[:keep]


def retrieve_for_role_li(
    store: Any,
    role: str,
    query: str,
    parent_id: int | None,
) -> list[Hit]:
    """Hybrid BM25 + vector → RRF → rerank using LlamaIndex retrievers.

    ``store`` accepts either a ``VectorStoreIndex`` or any object with a
    ``_index`` attribute holding one.  Passing a ``Store`` (store_v2) is
    also accepted — in that case the function falls back to building its
    own index via ``aiforge_core.rag.index.build_index`` using
    ``AIFORGE_PGMEM_DSN``.

    Return type is identical to ``retrieval.retrieve_for_role``.
    """
    if not query or not query.strip():
        return []

    if role not in ROLE_POLICIES:
        log.warning("unknown role %r; using 'planner' policy", role)
        role = "planner"

    policy = ROLE_POLICIES[role]

    index: Any
    try:
        from llama_index.core import VectorStoreIndex as _VSI

        if isinstance(store, _VSI):
            index = store
        elif hasattr(store, "_index"):
            index = store._index
        else:
            from aiforge_core.rag.index import build_index

            dsn = os.environ.get(
                "AIFORGE_PGMEM_DSN", "host=127.0.0.1 port=5432 dbname=aiforge"
            )
            index = build_index(dsn)
    except ImportError:
        index = store

    rankings: list[list[Hit]] = []
    for spec in policy["tiers"]:
        tier = spec["tier"]
        top_k = spec["top_k"]
        wing_prefix: str | None = spec.get("wing_prefix")
        if tier == "t1" and parent_id is not None:
            wing_prefix = f"ticket/{parent_id}"

        vec_hits = _vector_retrieve(index, query, top_k)
        bm25_hits = _bm25_retrieve(index, query, top_k)
        rankings.extend([vec_hits, bm25_hits])

    if not any(rankings):
        log.warning("retrieve_for_role_li: all rankings empty (role=%s)", role)
        return []

    top_n = sum(s["top_k"] for s in policy["tiers"])
    fused = rrf_fuse(rankings, k=60, top_n=top_n)
    return _rerank(query, fused, keep=policy["rerank_keep"])
