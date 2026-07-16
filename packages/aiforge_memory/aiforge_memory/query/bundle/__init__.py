"""ContextBundle builder — translator → Cypher → token-budget pack.

Public surface:
    bundle.query(text, *, repo, driver, role='doer', token_budget=4000)
        -> ContextBundle
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aiforge_memory.core.neo4j import vector_overfetch_k
from aiforge_memory.query import fastpath, translator

from ._helpers import _count_tokens
from ._model import ContextBundle
from ._hydrators import (
    _Q_DOMAINS,
    _Q_FLOWS,
    _SYM_FIELDS,
    _call_neighbours,
    _chunks_for,
    _cross_repo_for,
    _decisions_for,
    _docs_for,
    _domains_for,
    _files_rows,
    _flows_for,
    _notes_for,
    _observations_for,
    _repo_docs_for,
    _repo_map_for,
    _services_rows,
    _symbols_by_terminal_name,
    _symbols_rows,
    _vector_observations,
)
from ._builder import (
    _trim_to_budget,
    query,
)

__all__ = [
    "ContextBundle",
    "query",
]
