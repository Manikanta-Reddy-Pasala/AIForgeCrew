"""LlamaIndex-backed RAG package for AIForgeCrew Phase 2.

Exports the drop-in retrieval entry point used by memory.py when
``rag.backend=llamaindex``.
"""
from __future__ import annotations

from aiforge_core.rag.retriever import retrieve_for_role_li

__all__ = ["retrieve_for_role_li"]
