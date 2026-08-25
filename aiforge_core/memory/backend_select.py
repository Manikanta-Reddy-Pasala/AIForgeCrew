"""Pick the memory backend.

This is a SQLite-only build (embedded, zero-infra) — SQLite is the sole
memory backend, so these helpers are constant. Kept as a stable seam for
the callers that still ask which backend is active.
"""
from __future__ import annotations


def memory_backend() -> str:
    return "sqlite"


def embedded() -> bool:
    """True — the zero-infra SQLite memory backend is always active."""
    return True
