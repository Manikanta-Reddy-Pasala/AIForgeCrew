"""Pick the memory backend from the environment.

SQLite (embedded, zero-infra) is the default. The graph/SQL backends
take over only when their connection env vars are present, so a fresh
clone runs fully offline while an operator's NUC keeps Neo4j.

Resolution order:
  1. ``AIFORGE_MEMORY_BACKEND`` explicit value ('sqlite'|'neo4j'|'postgres')
  2. Neo4j env present (``AIFORGE_NEO4J_URI`` / ``NEO4J_URI``) -> 'neo4j'
  3. Postgres env present (``AIFORGE_PG_URL``) -> 'postgres'
  4. default -> 'sqlite'

Note: ``memory.neo4j_conn.neo4j_params`` defaults the URI to
``bolt://127.0.0.1:7687`` even when unset, so we must check raw env
presence here rather than that helper.
"""
from __future__ import annotations

import os

_VALID = {"sqlite", "neo4j", "postgres"}


def memory_backend() -> str:
    explicit = (os.environ.get("AIFORGE_MEMORY_BACKEND") or "").strip().lower()
    if explicit in _VALID:
        return explicit
    if os.environ.get("AIFORGE_NEO4J_URI") or os.environ.get("NEO4J_URI"):
        return "neo4j"
    if os.environ.get("AIFORGE_PG_URL"):
        return "postgres"
    return "sqlite"


def embedded() -> bool:
    """True when the zero-infra SQLite memory backend is active."""
    return memory_backend() == "sqlite"
