"""Single source of truth for Neo4j connection parameters.

Half the memory modules read ``AIFORGE_NEO4J_PASSWORD`` only while the
other half fall back to ``NEO4J_PASSWORD`` — so an operator exporting
just one name got learner_persist silently failing auth (logged as
``neo4j_unreachable``) while failure_memory connected fine. One helper,
one fallback chain, used by every driver-building call site.
"""
from __future__ import annotations

import os

_DEFAULT_URI = "bolt://127.0.0.1:7687"
_DEFAULT_USER = "neo4j"
_DEFAULT_PASSWORD = "password"


def neo4j_params() -> tuple[str, str, str]:
    """Return ``(uri, user, password)`` with the full fallback chain:
    ``AIFORGE_NEO4J_*`` → ``NEO4J_*`` → local-dev defaults."""
    uri = (os.environ.get("AIFORGE_NEO4J_URI")
           or os.environ.get("NEO4J_URI")
           or _DEFAULT_URI)
    user = (os.environ.get("AIFORGE_NEO4J_USER")
            or os.environ.get("NEO4J_USER")
            or _DEFAULT_USER)
    password = (os.environ.get("AIFORGE_NEO4J_PASSWORD")
                or os.environ.get("NEO4J_PASSWORD")
                or _DEFAULT_PASSWORD)
    return uri, user, password


__all__ = ["neo4j_params"]
