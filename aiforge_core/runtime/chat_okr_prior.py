"""The previous chat session in the same project, and the brief a new session
starts from."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_okr")


def _session_cwd(session_id) -> "str | None":
    """A session's stored cwd, or None. Never raises."""
    if session_id is None:
        return None
    try:
        from aiforge_core.runtime import chat_store
        s = chat_store.get_session(session_id) or {}
    except Exception:  # noqa: BLE001
        return None
    cwd = (s.get("cwd") or "").strip()
    return cwd or None


def _same_project(a: "str | None", b: "str | None") -> bool:
    """True when two session cwds are the SAME working tree. Two unpinned chats
    each get their own ``chat-workspaces/session-<id>`` dir, so they are NOT the
    same project — which is exactly the case that must not carry work forward."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:  # noqa: BLE001
        return a == b


def previous_session_id(exclude_session_id, *, cwd: "str | None" = None):
    """The id of the MOST RECENT prior session (excluding the current), or None.
    Used so recall can exclude exactly what previous_session_brief already
    injects — without dropping OLDER sessions' recall. With ``cwd``, applies the
    SAME same-project filter as :func:`previous_session_brief`, so the two never
    disagree about which session was carried forward. Never raises."""
    return _most_recent_prior_session(exclude_session_id, cwd=cwd)


def _most_recent_prior_session(exclude_session_id, *,
                               cwd: "str | None" = None) -> "int | None":
    """The id of the most recent session that is NOT ``exclude_session_id``, or
    None. When ``cwd`` is given, only sessions pinned to that SAME working tree
    qualify — a different project's session is never "the previous session".
    Soft-fails to None."""
    try:
        from aiforge_core.runtime import chat_store
        sessions = chat_store.list_sessions() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief list_sessions failed: %s", exc)
        return None
    for s in sessions:                       # list_sessions is newest-first
        sid = (s or {}).get("id")
        if sid is None or sid == exclude_session_id:
            continue
        if cwd and not _same_project((s or {}).get("cwd"), cwd):
            continue
        return sid
    return None


def previous_session_brief(exclude_session_id, *, cwd: "str | None" = None,
                           max_turns: int = 6, max_chars: int = 1200) -> str:
    """A short REFERENCE block from the most recent prior session in the SAME
    project, so a follow-up asked in a new chat still has its context.

    Two hard limits, both about not inheriting somebody else's job:

    * **Same project only.** Pass ``cwd`` (the current session's) and only a
      prior session pinned to that same working tree qualifies. Two unpinned
      chats live in their own ``chat-workspaces/session-<id>`` dirs, so nothing
      is carried between them — that path is how one chat's task (a repo it was
      editing) turned up as the next chat's work. Knowledge still crosses
      sessions, through memory recall and the ``memory_lookup`` /
      ``search_chat_sessions`` tools; only unasked-for TASK CONTINUATION stops.
    * **Notes, not a work order.** The block is framed as reference and
      supersedable: the model answers the user's current ask and does not resume,
      continue, or re-run anything described in it.

    Deterministic (no LLM — the tail of the prior transcript). Empty when there
    is no qualifying prior session. Never raises."""
    prior_id = _most_recent_prior_session(exclude_session_id, cwd=cwd)
    if prior_id is None:
        return ""
    try:
        from aiforge_core.runtime import chat_store
        msgs = chat_store.get_messages(prior_id) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief get_messages failed: %s", exc)
        return ""
    turns = [m for m in msgs if isinstance(m, dict)
             and m.get("role") in ("user", "assistant")
             and (m.get("content") or "").strip()]
    if not turns:
        return ""
    lines = [f"PREVIOUS SESSION {prior_id} — REFERENCE ONLY (same project). "
             "Notes and conclusions from an earlier conversation, for context. "
             "Do NOT resume, continue, or re-run any task described here, and "
             "do not edit files because of it — answer the user's CURRENT "
             "request only. If it contradicts the new ask, the new ask wins:"]
    for m in turns[-max(1, max_turns):]:
        role = (m.get("role") or "user").strip().upper()
        content = " ".join((m.get("content") or "").split())
        lines.append(f"{role}: {content}")
    return "\n".join(lines)[:max_chars]
