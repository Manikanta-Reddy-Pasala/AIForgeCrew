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

from aiforge_core.rag.indexer import (  # noqa: F401
    ReindexResult,
    reindex_repo,
)
from aiforge_core.rag.retriever import retrieve_for_role_li  # noqa: F401

__all__ = [
    "ReindexResult",
    "reindex_repo",
    "retrieve_for_role_li",
]
