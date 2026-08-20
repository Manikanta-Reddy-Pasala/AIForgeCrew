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


def _max_windows() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_SESSION_COMPACT_MAX_WINDOWS", "20")))
    except (TypeError, ValueError):
        return 20


def fold_sync(session_id: int) -> dict:
    """Fold one session NOW (blocking), walking its WHOLE backlog. Never raises.

    Used by the delete path, which must fold before the rows are removed —
    that caller is NOT window-gated (the turns are about to be destroyed, so
    "wait until 18:00" would mean "lose it"). It walks at most
    ``_max_windows()``; with the on-switch fold now deferred to the evening,
    a very long session deleted mid-day can exceed that and lose its tail —
    raise AIFORGE_SESSION_COMPACT_MAX_WINDOWS if that matters to you. One
    ``compact_session`` only distils the turns that fit in one window, so a
    single call would delete the rest of a long chat unfolded — and since
    compaction moved to one pass a day that backlog is a day's worth, not the
    ≤30 minutes the old idle daemon left.
    """
    try:
        from aiforge_core.runtime import chat_okr
        repo = _repo_for(session_id)
        out: dict = {}
        captured = 0
        for _ in range(_max_windows()):
            out = chat_okr.compact_session(session_id, repo=repo) or {}
            captured += int(out.get("captured") or 0)
            if out.get("skipped") or not out.get("ok") or not out.get("remaining"):
                break
        return dict(out, captured=captured)
    except Exception as exc:  # noqa: BLE001 — a fold must never break the turn
        # WARNING (not debug): a fold runs in a daemon thread whose exceptions
        # are otherwise invisible, so a persistently broken fold must be
        # diagnosable at the default level.
        log.warning("fold_sync failed for session %s: %s", session_id, exc)
        return {"ok": False, "error": str(exc), "captured": 0}


def fold_async(session_id: int | None) -> None:
    """Best-effort background fold — returns immediately. No-op when disabled,
    when there is no session id, or OUTSIDE the compaction window.

    The window matters: this fires whenever the user opens a new chat, so
    without it the LLM-heavy fold runs at 09:00 on a machine whose operator
    pinned compaction to 18:00. Nothing is lost by waiting — the daily pass
    walks every session with new turns, and ``compact_session`` is
    offset-based, so the evening pass picks up exactly what this skipped.
    """
    if session_id is None or not _enabled():
        return
    from aiforge_core.runtime import compact_window
    _hour = compact_window.at_hour()
    if not compact_window.open_now():
        log.info("fold for session %s deferred to the compaction window (%02d:00)",
                 session_id, _hour or 0)
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
