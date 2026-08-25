"""Pick the ticket storage backend once per process.

SQLite-only build — always the embedded SQLite backend (AIFORGE_DB_PATH).
"""
from __future__ import annotations

import threading

from aiforge_core.config.env import AIFORGE_DB_PATH

_LOCK = threading.Lock()
_BACKEND = None


def get_backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is None:
            _BACKEND = _make_backend()
    return _BACKEND


def _make_backend():
    from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
    be = SqliteBackend(AIFORGE_DB_PATH)
    be.ensure_schema()
    return be


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None
