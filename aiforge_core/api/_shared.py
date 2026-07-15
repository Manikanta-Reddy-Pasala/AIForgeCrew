"""Small cross-cutting helpers shared by api.py and the api/routes/ modules.

Kept dependency-light (stdlib + env) so route modules can import it without
pulling api.py back in (which would be circular). Add a helper here only when
it's used by BOTH api.py and a route module.
"""
from __future__ import annotations

import os


def env_truthy(name: str) -> bool:
    """True when env var ``name`` is set to a truthy string (1/true/yes/on)."""
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


__all__ = ["env_truthy"]
