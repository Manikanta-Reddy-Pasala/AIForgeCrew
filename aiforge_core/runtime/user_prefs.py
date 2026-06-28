"""Durable user-preference memory (frontier gap #9).

A single GLOBAL self-editing block (repo ``_user``, label ``preferences``)
that persists user preferences across every repo and ticket — distinct
from the per-session rule_capture and the per-repo working_notes block.
Recorded preferences are injected into the orchestrator/doer context so
the agent honours "I always want X" without being re-told.

Reuses AiForgeMemory ``features.memory.blocks``; neo4j-only, soft-fail.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiforge.user_prefs")

_REPO = "_user"
_LABEL = "preferences"


def _driver():
    try:
        from .learner_persist import _open_driver
        return _open_driver()
    except Exception:  # noqa: BLE001
        return None


def get_preferences() -> str:
    """Current user-preference text, or '' (never raises)."""
    drv = _driver()
    if drv is None:
        return ""
    try:
        from aiforge_memory.features.memory import blocks
        return blocks.get_block(drv, repo=_REPO, label=_LABEL)
    except Exception as exc:  # noqa: BLE001
        log.debug("get_preferences failed: %s", exc)
        return ""
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def record_preference(text: str) -> dict:
    """Append a durable user preference (deduped). Soft-fail."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    drv = _driver()
    if drv is None:
        return {"ok": False, "error": "no neo4j driver"}
    try:
        from aiforge_memory.features.memory import blocks
        r = blocks.append_block(drv, repo=_REPO, label=_LABEL, line=text)
        return {"ok": True, "chars": r.get("chars", 0)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            drv.close()
        except Exception:  # noqa: BLE001
            pass


def preferences_block() -> str:
    """Render the prefs as an injectable context block, or '' when none."""
    prefs = get_preferences()
    if not prefs.strip():
        return ""
    return ("USER PREFERENCES (durable — honour these without being "
            "re-told):\n" + prefs.strip())


__all__ = ["get_preferences", "record_preference", "preferences_block"]
