"""Unified memory query — one tool, all sources, ranked + merged.

KISS: single ``query(text, *, ticket=None, role=None, limit=8)`` call
fans out to every memory backend in parallel and returns one ranked
list. Replaces the 3-tool chain ``search_memory → ticket_brief →
related_memories`` (and often ``sym_lookup`` / ``find_doc`` on top).

Sources merged (each contributes, soft-fail individually):
1. embedded SQLite vector recall
2. ticket_brief (if ``ticket`` looks like an identifier)
3. sym_lookup MCP — code symbol + signature
4. find_doc MCP — markdown / SOP files
5. docs_index.lookup_doc — external library docs (top library guessed
   from query: spring/react/mongodb/...)

Ranking: each source emits a ``score`` (roughly [0, 1]); we min-max
normalise it per source (so each source's top hit maps to its weight
ceiling — comparable across sources), multiply by the per-source weight
(tunable via ``AIFORGE_UMEM_WEIGHT_<source>``), then dedup identical
content across sources and take top-K. Normalisation is monotonic within
a source, so within-source order is unchanged; disable via
``AIFORGE_UMEM_NORMALIZE=0`` to restore the legacy weight-scaled scores.

Public surface:
- ``query(text, *, ticket=None, role=None, limit=8) -> dict``

This module was split (grouped by concern) into ``_helpers`` / ``_ranking``
/ ``_sources`` / ``_query`` submodules; this package re-exports the full
former top-level surface so ``from aiforge_core.memory import unified_query``
and every ``unified_query.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._helpers import (
    _DEFAULT_WEIGHTS,
    _LIBRARY_HINTS,
    _QCACHE,
    _QCACHE_MAX,
    _SYMBOL_HINT_RE,
    _TICKET_RE,
    _extract_symbol,
    _guess_library,
    _looks_like_symbol,
    _qcache_ttl,
    _resolve_weights,
    _tag,
)
from ._query import query, render
from ._ranking import (
    _abs_weight,
    _dedup,
    _diversify,
    _normalize_scores,
    _raw_of,
    _rerank_top,
)
from ._sources import (
    _afm_bundle,
    _chat_sessions,
    _cross_repo_links,
    _docs_lookup,
    _global_vector_recall,
    _mcp_call,
    _ticket_brief,
    _ticket_local,
    _unpack_mcp_rows,
)

__all__ = ["query", "render"]
