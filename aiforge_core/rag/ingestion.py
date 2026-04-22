"""LlamaIndex ingestion pipeline for bulk T4 reindex.

NOT wired into retain_fact (Phase 2 bonus only).  Use ingest_chunk
from a dedicated reindex script; the live retain_fact path continues
to use store_v2.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llama_index.core import VectorStoreIndex


def ingest_chunk(
    tier: str,
    wing: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    *,
    index: VectorStoreIndex | None = None,
    dsn: str | None = None,
) -> None:
    """Parse *text* into nodes and upsert into the vector store.

    Parameters
    ----------
    tier:
        One of ``t1``..``t4``.
    wing:
        Wing label, e.g. ``code/myrepo``.
    text:
        Raw chunk text.
    metadata:
        Extra metadata merged into every generated node.
    index:
        Pre-built ``VectorStoreIndex``.  If *None*, one is built from
        *dsn* (or ``AIFORGE_PGMEM_DSN`` env var).
    dsn:
        libpq DSN string.  Ignored when *index* is provided.
    """
    from llama_index.core import Document, VectorStoreIndex as _VSI
    from llama_index.core.node_parser import SimpleNodeParser

    if index is None:
        from aiforge_core.rag.index import build_index

        resolved_dsn = dsn or os.environ.get(
            "AIFORGE_PGMEM_DSN", "host=127.0.0.1 port=5432 dbname=aiforge"
        )
        index = build_index(resolved_dsn, tier_filter=tier)

    meta: dict[str, Any] = {"tier": tier, "wing": wing, **(metadata or {})}
    doc = Document(text=text, metadata=meta)
    parser = SimpleNodeParser.from_defaults()
    nodes = parser.get_nodes_from_documents([doc])
    for node in nodes:
        node.metadata.update(meta)
    index.insert_nodes(nodes)
