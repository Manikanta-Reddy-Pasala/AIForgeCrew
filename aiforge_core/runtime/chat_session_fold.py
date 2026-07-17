"""Fold a chat session's knowledge into memory on switch / close.

Historically a session was only OKR-compacted by the idle periodic scan (~30
min later, lost on restart) or a manual ``POST .../compact`` — so switching
away from a chat left its knowledge unfolded, and DELETING a chat lost it
entirely. This wires the fold into the two lifecycle moments that matter:

* closing/deleting a chat — fold BEFORE the rows go, or the knowledge is gone;
* opening a NEW chat — fold the session you just moved away from.

:func:`fold_async` runs :func:`chat_okr.compact_session` in a daemon thread so
the HTTP handler returns immediately. ``compact_session`` is idempotent (durable
per-session offset) and never raises, so a redundant call is a cheap no-op.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("aiforge.chat_fold")


def _enabled() -> bool:
    return os.environ.get("AIFORGE_SESSION_COMPACT_ON_SWITCH", "1") not in ("0", "false")


def _repo_for(session_id: int) -> str | None:
    try:
        from aiforge_core.runtime import chat_store
        from aiforge_core.runtime.chat_agent import _chat_repo_key
        sess = chat_store.get_session(session_id) or {}
        cwd = sess.get("cwd")
        return _chat_repo_key(cwd) if cwd else None
    except Exception:  # noqa: BLE001
        return None


def fold_sync(session_id: int) -> dict:
    """Fold one session NOW (blocking). Used by the delete path, which must
    fold before the rows are removed. Never raises."""
    try:
        from aiforge_core.runtime import chat_okr
        return chat_okr.compact_session(session_id, repo=_repo_for(session_id))
    except Exception as exc:  # noqa: BLE001 — a fold must never break the turn
        log.debug("fold_sync failed for session %s: %s", session_id, exc)
        return {"ok": False, "error": str(exc), "captured": 0}


def fold_async(session_id: int | None) -> None:
    """Best-effort background fold — returns immediately. No-op when disabled or
    no session id."""
    if session_id is None or not _enabled():
        return
    threading.Thread(
        target=fold_sync, args=(session_id,),
        name=f"chat-fold-{session_id}", daemon=True).start()


def fold_previous_async(new_session_id: int | None) -> None:
    """On opening a NEW chat, fold the session the user just moved away from —
    the most-recent OTHER session. Best-effort, background."""
    if not _enabled():
        return
    try:
        from aiforge_core.runtime import chat_okr
        prev = chat_okr.previous_session_id(new_session_id)
    except Exception:  # noqa: BLE001
        prev = None
    if prev is not None:
        fold_async(prev)
