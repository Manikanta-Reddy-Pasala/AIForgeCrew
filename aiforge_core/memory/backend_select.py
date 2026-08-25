"""Pick the memory backend from the environment.

SQLite (embedded, zero-infra) is the default. The Postgres SQL backend
takes over only when its connection env var is present, so a fresh
clone runs fully offline.

Resolution order:
  1. ``AIFORGE_MEMORY_BACKEND`` explicit value ('sqlite'|'postgres')
  2. Postgres env present (``AIFORGE_PG_URL``) -> 'postgres'
  3. default -> 'sqlite'

An unknown explicit value falls through to 'sqlite'.
"""
from __future__ import annotations

import os

_VALID = {"sqlite", "postgres"}


def memory_backend() -> str:
    explicit = (os.environ.get("AIFORGE_MEMORY_BACKEND") or "").strip().lower()
    if explicit in _VALID:
        return explicit
    if os.environ.get("AIFORGE_PG_URL"):
        return "postgres"
    return "sqlite"


def embedded() -> bool:
    """True when the zero-infra SQLite memory backend is active."""
    return memory_backend() == "sqlite"
