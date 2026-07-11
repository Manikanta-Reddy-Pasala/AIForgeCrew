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
            _BACKEND = _make_backend()
    return _BACKEND


def _make_backend():
    from aiforge_core.tickets.backends.sqlite_backend import SqliteBackend
    if AIFORGE_USE_SQLITE:
        be = SqliteBackend(AIFORGE_DB_PATH)
        be.ensure_schema()
        return be
    # Postgres configured (AIFORGE_PG_URL set — e.g. hybrid mode pointed the host
    # at a dockerized PG). If it's actually UNREACHABLE (no Docker on this box,
    # PG down), DEGRADE to embedded SQLite instead of crash-looping the runner —
    # a fresh/no-Docker machine then just works. Set AIFORGE_REQUIRE_PG=1 to
    # hard-fail instead of falling back.
    from aiforge_core.tickets.backends.pg_backend import PgBackend
    be = PgBackend(AIFORGE_PG_URL)
    try:
        be.ensure_schema()
        return be
    except Exception as exc:  # noqa: BLE001 — connection/refused/timeout
        import os
        if os.environ.get("AIFORGE_REQUIRE_PG") == "1":
            raise
        import logging
        logging.getLogger("aiforge.tickets").warning(
            "Postgres unreachable (%s) — falling back to embedded SQLite at %s. "
            "Run './run.sh --lite' for a zero-Docker box, or start Postgres.",
            exc, AIFORGE_DB_PATH)
        be = SqliteBackend(AIFORGE_DB_PATH)
        be.ensure_schema()
        return be


def reset_backend_for_tests():
    """Test hook — drop the memoized backend so env changes take effect."""
    global _BACKEND
    _BACKEND = None
