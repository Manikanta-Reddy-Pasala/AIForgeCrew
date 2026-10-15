"""Temporary chats: ask the model, get the answer, keep nothing.

A normal chat leaves things everywhere: memory notes and facts, captured
rules and preferences, session summaries and OKR briefs, the trace files,
the chat log, the carried working transcript, checkpoint commits, a git
worktree and its branch, the next-step history, offloaded context, Langfuse
traces. A temporary chat writes none of it:

* the chat row is marked ``temporary`` (and ``learn`` off) when it is made;
* every write path asks :func:`is_temporary` / :func:`current` and skips;
* the agent runs read-only (it can read files and look things up; nothing
  it can call writes) and as one agent (no team, no parallel subtasks, no
  side tasks that would outlive it);
* other chats never see it (no "previous session" brief, no search hit);
* deleting it (the UI deletes it when it is closed or left) removes the
  row, its messages and its scratch folder; a sweep removes any left over
  (at startup, and once one has been idle for a while).

The messages live in the chat database while the chat is open — the page and
the agent read them from there — and nowhere else.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("aiforge.chat_private")

_LOCK = threading.Lock()
_KNOWN: dict = {}          # session id → temporary? (a chat's flag never changes)


def is_temporary(session_id) -> bool:
    """Is chat ``session_id`` a temporary one? A chat's flag never changes and
    is cached when the chat is made (:func:`remember`), so this is mostly a
    dict read. A deleted temporary chat stays known as one (work of its last
    turn that finishes after the delete must write nothing). Not cached: the
    row says (a side task of a temporary chat is temporary too). Unknown or
    unreadable: False."""
    if session_id is None:
        return False
    key = str(session_id)
    with _LOCK:
        if key in _KNOWN:
            return _KNOWN[key]
    try:
        from aiforge_core.runtime import chat_store
        sess = chat_store.get_session(int(session_id))
        if not sess:
            return False
        val = bool(sess.get("temporary"))
        if not val and sess.get("parent_id"):
            val = bool((chat_store.get_session(int(sess["parent_id"])) or {})
                       .get("temporary"))
    except Exception:  # noqa: BLE001 — cannot tell: a normal chat
        return False
    with _LOCK:
        if len(_KNOWN) > 4096:
            # keep the temporary ones: they are the ones that must not leak
            for k in [k for k, v in _KNOWN.items() if not v]:
                del _KNOWN[k]
        _KNOWN[key] = val
    return val


def current() -> bool:
    """The chat this request / turn runs for is a temporary one. Only the
    session bound to THIS context counts (never the process-wide "last chat
    that ran", which a bare thread would otherwise read)."""
    sid = None
    try:
        from aiforge_core.runtime import chat_cancel
        sid = chat_cancel.active()
    except Exception:  # noqa: BLE001
        sid = None
    if sid is None:
        try:
            from aiforge_core.runtime import request_context
            sid = request_context.context_session_id()
        except Exception:  # noqa: BLE001
            sid = None
    return is_temporary(sid)


def remember(session_id, temporary: bool) -> None:
    """The flag of a chat just made (it replaces whatever an id reused after
    a reset had)."""
    with _LOCK:
        _KNOWN[str(session_id)] = bool(temporary)


def forget(session_id) -> None:
    """The chat is deleted. A temporary chat's id stays known as temporary,
    so work of its turn that finishes after the delete still writes nothing
    (the row is gone and could not say so)."""
    with _LOCK:
        if not _KNOWN.get(str(session_id)):
            _KNOWN.pop(str(session_id), None)


def forget_all() -> None:
    """Drop every cached flag (tests with a fresh store)."""
    with _LOCK:
        _KNOWN.clear()


def _idle_s() -> float:
    try:
        return max(60.0, float(os.environ.get("AIFORGE_TEMP_CHAT_IDLE_S", str(6 * 3600))))
    except ValueError:
        return 6 * 3600.0


def sweep(*, all_of_them: bool = False) -> int:
    """Delete temporary chats left behind (the page was closed without
    leaving the chat): at startup every one, later those idle longer than
    ``AIFORGE_TEMP_CHAT_IDLE_S`` (6 h). A chat with a turn running is left.
    Returns how many were deleted."""
    import datetime as _dt
    try:
        from aiforge_core.runtime import chat_runs, chat_store
        rows = [s for s in chat_store.list_sessions() or [] if s.get("temporary")]
    except Exception as exc:  # noqa: BLE001
        log.debug("temporary chat sweep skipped: %s", exc)
        return 0
    now = _dt.datetime.now(_dt.timezone.utc)
    gone = 0
    for s in rows:
        if chat_runs.is_running(s["id"]):
            continue
        if not all_of_them:
            try:
                upd = _dt.datetime.fromisoformat(str(s.get("updated_at")).replace("Z", "+00:00"))
                if upd.tzinfo is None:
                    upd = upd.replace(tzinfo=_dt.timezone.utc)
                if (now - upd).total_seconds() < _idle_s():
                    continue
            except (TypeError, ValueError):
                pass
        try:
            from aiforge_core.api.routes._chat._sessions import chat_session_delete
            chat_session_delete(int(s["id"]))
            gone += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("temporary chat %s not deleted: %s", s.get("id"), exc)
    if gone:
        log.info("temporary chats deleted: %d", gone)
    return gone


__all__ = ["current", "forget", "forget_all", "is_temporary", "remember", "sweep"]
