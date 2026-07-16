"""Cypher writer for the memory layer — Decision_v2, Observation_v2,
Note_v2, Doc_v2 + MENTIONS / SUPERSEDES / RECORDS edges.

Public surface:
    upsert_decision(driver, *, repo, **fields)        -> dict
    upsert_observation(driver, *, repo, **fields)     -> dict
    upsert_note(driver, *, repo, **fields)            -> dict
    upsert_doc(driver, *, repo, **fields)             -> dict
    forget(driver, *, repo, node_id, label)           -> dict
    list_memory(driver, *, repo, label=None, limit=50) -> list[dict]
    recall_observations(driver, *, repo, query_vec, k=10) -> list[dict]

Idempotent: keyed on caller-supplied id (uuid4 if omitted). Auto-stamps
created_at, updated_at, schema_version. References (refs=[fqname|path])
become MENTIONS edges to Symbol_v2 / File_v2 nodes if they exist.

Memory nodes are *additive* — they coexist with the code graph and can
be retrieved either directly (by id) or via vector recall over
Observation embeddings.

This module was split (grouped by concern) into ``_helpers`` /
``_entities`` / ``_write`` / ``_maintenance`` / ``_recall`` submodules;
this package re-exports the full former top-level surface so
``from aiforge_memory.features.memory import store`` and every
``store.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._entities import (
    _CODE_EXTS,
    _RE_ENV,
    _RE_FILE,
    _RE_SYMBOL,
    _RE_TICKET,
    _RE_URL,
    extract_entities,
)
from ._helpers import (
    _ALLOWED_LABELS,
    _SCHEMA_VERSION,
    _clamp01,
    _link_refs,
    _new_id,
    _text_hash,
)
from ._maintenance import (
    _INVALIDATE_OBSERVATION,
    forget,
    invalidate_observation,
    list_memory,
    restore,
    soft_forget,
)
from ._recall import (
    _RECALL_OBSERVATION,
    _RECALL_OBSERVATIONS_PPR,
    find_semantic_dup,
    recall_observations,
    recall_observations_ppr,
    rerank_by_recency,
)
from ._write import (
    _FIND_DUP_OBSERVATION,
    _SUPERSEDE_OBSERVATION,
    _TOUCH_DUP_OBSERVATION,
    _TOUCH_DUP_OBSERVATION_NO_APOC,
    _UPSERT_DECISION,
    _UPSERT_DOC,
    _UPSERT_NOTE,
    _UPSERT_OBSERVATION,
    upsert_decision,
    upsert_doc,
    upsert_note,
    upsert_observation,
)

__all__ = [
    "upsert_decision",
    "upsert_observation",
    "upsert_note",
    "upsert_doc",
    "forget",
    "soft_forget",
    "restore",
    "list_memory",
    "invalidate_observation",
    "recall_observations",
    "find_semantic_dup",
    "recall_observations_ppr",
    "rerank_by_recency",
    "extract_entities",
]
