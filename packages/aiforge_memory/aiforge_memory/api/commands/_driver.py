"""Shared Neo4j driver helper.

Every command that talks to Neo4j goes through here; the actual env
resolution lives in :func:`aiforge_memory.core.neo4j.open_driver` so a
connection config change is a one-file edit.
"""
from __future__ import annotations

from aiforge_memory.core.neo4j import open_driver


def driver():
    """Open the project's Neo4j driver. Errors propagate to caller."""
    return open_driver()


__all__ = ["driver"]
