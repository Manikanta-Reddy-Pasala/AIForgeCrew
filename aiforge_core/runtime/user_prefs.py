"""Durable user-preference memory (frontier gap #9).

A single GLOBAL self-editing block (repo ``_user``, label ``preferences``)
that persists user preferences across every repo and ticket.

This was backed by the optional AiForgeMemory graph blocks store, which has
been removed (SQLite-only build). The public surface is preserved as a
soft no-op so callers that inject preferences degrade gracefully.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.user_prefs")

_REPO = "_user"
_LABEL = "preferences"


def _driver():
    """The preferences block backend was removed — always None."""
    return None


def get_preferences() -> str:
    """Current user-preference text, or '' (never raises)."""
    return ""


def record_preference(text: str) -> dict:
    """Append a durable user preference. The backend was removed, so this is
    a soft no-op."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    return {"ok": False, "error": "preferences backend removed"}


def preferences_block() -> str:
    """Render the prefs as an injectable context block, or '' when none."""
    prefs = get_preferences()
    if not prefs.strip():
        return ""
    return ("USER PREFERENCES (durable — honour these without being "
            "re-told):\n" + prefs.strip())


__all__ = ["get_preferences", "record_preference", "preferences_block"]
