"""Feature flag reader for AIForgeCrew.

Reads from env var ``AIFORGE_FLAG_<NAME_UPPER_UNDERSCORED>`` where the name
has dots replaced with underscores and letters uppercased.

Example:
    ``get_flag("doer.backend")`` reads ``AIFORGE_FLAG_DOER_BACKEND``.
"""
from __future__ import annotations

import os


def get_flag(name: str, default: str = "legacy") -> str:
    """Return the feature flag value for *name*, falling back to *default*."""
    env_key = "AIFORGE_FLAG_" + name.upper().replace(".", "_")
    return os.environ.get(env_key, default)
