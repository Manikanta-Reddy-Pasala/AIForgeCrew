"""LlamaIndex-backed RAG package for AIForgeCrew Phase 2.

Exports:
- ``retrieve_for_role_li`` — hybrid BM25 + vector retriever used by
  memory.py when ``rag.backend=llamaindex``.
- ``reindex_repo`` / ``ReindexResult`` — AST-chunked upsert into
  store_v2 T4, used by ``scripts/runtime/reindex-daily.py``.

(Pre-2026-04-24, indexer lived at ``aiforge_core/rag.py`` which was
shadowed by the package — callers saw ImportError. Moved into the
package so the imports resolve.)
"""
from __future__ import annotations

import os as _os

from aiforge_core.rag.indexer import (  # noqa: F401
    ReindexResult,
    reindex_repo,
)

# Swap memory backend at import time based on env.
#   AIFORGE_MEMORY_BACKEND=postgres  (default, legacy path via LlamaIndex/pg)
#   AIFORGE_MEMORY_BACKEND=neo4j     (Option A: all memory via Neo4j)
_backend = _os.environ.get("AIFORGE_MEMORY_BACKEND", "postgres").lower()
if _backend == "neo4j":
    from aiforge_core.rag.neo4j_memory import (  # noqa: F401
        retrieve_for_role_li,
        retain_fact,
        write_t1,
    )
else:
    from aiforge_core.rag.retriever import retrieve_for_role_li  # noqa: F401

__all__ = [
    "ReindexResult",
    "reindex_repo",
    "retrieve_for_role_li",
]
