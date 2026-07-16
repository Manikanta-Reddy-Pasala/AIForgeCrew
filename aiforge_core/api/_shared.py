"""Small cross-cutting helpers shared by api.py and the api/routes/ modules.

Kept dependency-light (stdlib + env, plus the psycopg driver already required
everywhere) so route modules can import it without pulling api.py back in (which
would be circular). Add a helper here only when it's used by BOTH api.py and a
route module.
"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from aiforge_core.config.env import AIFORGE_DSN


def env_truthy(name: str) -> bool:
    """True when env var ``name`` is set to a truthy string (1/true/yes/on)."""
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


def _db():
    return psycopg.connect(AIFORGE_DSN, row_factory=dict_row, connect_timeout=5,
                           options="-c statement_timeout=10000")


__all__ = ["env_truthy", "_db"]
