"""Embedded SQLite memory store — zero-infra observation/recall.

Stores memory units (the agent's learnings, failures, self-writes) in
``~/.aiforge/memory.db`` with an offline hash embedding, and recalls by
brute-force cosine. This is the memory backend (``backend_select.embedded()``
is True). Quality is lexical, not semantic.

Public surface:
    write_unit(*, text, kind, ...) -> int        # 0 when skipped (dup/empty)
    recall(text, *, limit, repo) -> list[dict]
    stats() -> dict

This module was split (grouped by concern) into ``_schema`` / ``_write`` /
``_keyword`` / ``_recall`` / ``_maintenance`` submodules; this package
re-exports the full former top-level surface so
``from aiforge_core.memory import sqlite_memory`` and every
``sqlite_memory.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._schema import (
    _DDL,
    _EMBED_WARNED,
    _FTS_DDL,
    _LOCK,
    _VEC_TRIGGERS,
    _conn,
    _db_path,
    _init_vec,
    _log,
    _safe_embed,
    _vec_enabled,
)
from ._write import (
    clear,
    delete_by_source,
    delete_by_tag,
    delete_by_text_contains,
    delete_stale_compacted_notes,
    prune_missing_file_rows,
    source_text_unchanged,
    upsert_by_tag,
    write_unit,
)
from ._keyword import (
    _STOP,
    _VOCAB_CACHE,
    _fts_rows,
    _kw_tokens,
    _re,
    _spell_correct,
    _vocab,
    keyword_search,
)
from ._recall import (
    _vec_recall,
    recall,
    recent,
)
from ._maintenance import (
    _get_meta,
    dedupe,
    reembed_all,
    stats,
    stored_dim_mismatch,
    stored_embedder_changed,
)

__all__ = [
    "write_unit",
    "keyword_search",
    "delete_by_tag",
    "upsert_by_tag",
    "recall",
    "recent",
    "delete_stale_compacted_notes",
    "prune_missing_file_rows",
    "stored_dim_mismatch",
    "stored_embedder_changed",
    "reembed_all",
    "source_text_unchanged",
    "delete_by_source",
    "delete_by_text_contains",
    "dedupe",
    "clear",
    "stats",
]
