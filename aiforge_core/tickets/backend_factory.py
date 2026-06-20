"""Pick the ticket storage backend once per process.

SQLite by default (AIFORGE_DB_PATH); Postgres when AIFORGE_PG_URL set.
"""
from __future__ import annotations

import threading

from aiforge_core.config.env import AIFORGE_PG_URL, AIFORGE_DB_PATH, AIFORGE_USE_SQLITE

_LOCK = threading.Lock()
_BACKEND = None


def get_backend():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is None:
            if AIFORGE_USE_SQLITE:
                from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
                be = SqliteBackend(AIFORGE_DB_PATH)
            else:
                from aiforge_core.tickets.backends.pg_backend import PgBackend
                be = PgBackend(AIFORGE_PG_URL)
            be.ensure_schema()
            _BACKEND = be
    return _BACKEND


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None
