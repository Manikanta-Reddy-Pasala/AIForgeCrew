"""Session-end OKR compaction: a chat session → scoped OKR briefs.

:mod:`chat_summary` keeps ONE browsable summary per session; this module does
the KNOWLEDGE fold. At session end (idle timeout or an explicit request) the
transcript is distilled by the learner LLM into ATOMIC durable items — decisions,
learnings, gotchas, config/stack facts, tickets, and MEANINGFUL user inputs
(preferences/corrections/instructions) — with chit-chat and transient status
dropped. Each item is routed to its scope (global / project / topic) via
:func:`md_store.classify_scope` and folded into the matching OKR brief through
:func:`md_store.capture` (which maintains ``compacted-<scope>.md``).

Trigger is config-driven (``AIFORGE_SESSION_COMPACT`` = ``idle`` | ``turns`` |
``explicit`` | ``off``); this module is the DOER — the daemon/endpoint decides
WHEN to call it. Soft-fail everywhere: background upkeep must never raise.

Env:
  AIFORGE_SESSION_COMPACT        idle (default) | turns | explicit | off
  AIFORGE_SESSION_COMPACT_CHARS  transcript char cap (default 8000)
  AIFORGE_SESSION_COMPACT_MAX_TOKENS  extraction reply cap (default 1500)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_okr")

_EXTRACT_SYS = (
    "You distil a chat session between an engineer and an AI assistant into "
    "ATOMIC durable knowledge items worth remembering across sessions. Keep "
    "ONLY meaningful content: decisions, conventions, learnings, gotchas, "
    "config/stack facts, tickets worked, and MEANINGFUL user inputs "
    "(preferences, corrections, instructions the user gave). DROP pleasantries, "
    "small talk, transient status, and anything trivial or already obvious. "
    "Each item is ONE concise sentence tagged with a kind:\n"
    "  learning          a general lesson (applies across projects)\n"
    "  project_learning  a lesson about ONE repository/service\n"
    "  topic_learning    a lesson about a cross-cutting theme/workflow\n"
    "  user_comment      a meaningful thing the USER said to keep (intent/preference)\n"
    "Return an items list; empty if nothing durable was said."
)


def _disabled() -> bool:
    return os.environ.get("AIFORGE_SESSION_COMPACT", "idle") in (
        "off", "0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _transcript(turns: list[dict], limit: int) -> str:
    """Compact ``ROLE: content`` transcript, keeping the LAST ``limit`` chars
    (the tail carries the outcome/decisions)."""
    lines = []
    for m in turns:
        role = (m.get("role") or "user").strip().upper()
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    text = "\n\n".join(lines)
    return text[-limit:] if len(text) > limit else text


def _extract(transcript: str, role: str) -> list:
    """LLM → list of items (each ``.text`` + ``.kind``). Empty on any failure —
    this is also the MEANINGFUL-input filter (the prompt drops chit-chat)."""
    try:
        from pydantic import BaseModel

        from aiforge_core.llm.structured import structured_complete

        class SessionItem(BaseModel):
            text: str = ""
            kind: str = "learning"

        class SessionItems(BaseModel):
            items: list[SessionItem] = []

        res = structured_complete(
            role,
            [{"role": "system", "content": _EXTRACT_SYS},
             {"role": "user", "content": transcript[:12000]}],
            SessionItems,
            max_tokens=_int_env("AIFORGE_SESSION_COMPACT_MAX_TOKENS", 1500),
            max_retries=1, temperature=0.0)
        return list(getattr(res, "items", None) or [])
    except Exception as exc:  # noqa: BLE001 — model down → nothing this pass
        log.debug("chat_okr extract failed: %s", exc)
        return []


def compact_session(session_id, *, repo: str | None = None,
                    role: str = "learner", min_turns: int = 2) -> dict:
    """Distil ONE session into scoped OKR briefs. Never raises.

    Returns ``{"ok": bool, "captured": int, ...}`` — ``skipped`` for disabled /
    too-short sessions."""
    if _disabled():
        return {"ok": False, "skipped": "disabled", "captured": 0}
    try:
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import chat_store
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "captured": 0}
    try:
        messages = chat_store.get_messages(session_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "captured": 0}

    turns = [m for m in (messages or [])
             if isinstance(m, dict) and m.get("role") in ("user", "assistant")
             and (m.get("content") or "").strip()]
    if len(turns) < max(1, int(min_turns)):
        return {"ok": True, "skipped": "too_short", "captured": 0}

    transcript = _transcript(turns, _int_env("AIFORGE_SESSION_COMPACT_CHARS", 8000))
    items = _extract(transcript, role)
    captured = 0
    for it in items:
        text = (getattr(it, "text", "") or "").strip()
        if not text:
            continue
        kind = (getattr(it, "kind", "") or "learning").strip()
        try:
            sc = md_store.classify_scope(text, hint_repo=repo, role=role)
            md_store.capture(kind, text, repo=sc["repo"], topic=sc["topic"],
                             classify=False, source=f"chat-session:{session_id}")
            captured += 1
        except Exception as exc:  # noqa: BLE001 — one bad item never aborts the fold
            log.debug("chat_okr capture failed: %s", exc)
    log.info("chat_okr: session=%s repo=%s captured=%d", session_id, repo, captured)
    return {"ok": True, "captured": captured}


def previous_session_brief(exclude_session_id, *, max_turns: int = 6,
                           max_chars: int = 1200) -> str:
    """A short continuity block from the MOST RECENT prior session (excluding the
    current one) so the next chat carries the previous conversation forward. The
    block is explicitly framed as supersedable — if the user's new ask
    contradicts it, the new statement wins (and the OKR supersede policy applies
    at the next compaction). Deterministic (no LLM — the tail of the prior
    transcript). Empty when there is no prior session. Never raises."""
    try:
        from aiforge_core.runtime import chat_store
        sessions = chat_store.list_sessions() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief list_sessions failed: %s", exc)
        return ""
    prior_id = None
    for s in sessions:                       # list_sessions is newest-first
        sid = (s or {}).get("id")
        if sid is None or sid == exclude_session_id:
            continue
        prior_id = sid
        break
    if prior_id is None:
        return ""
    try:
        msgs = chat_store.get_messages(prior_id) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("previous_session_brief get_messages failed: %s", exc)
        return ""
    turns = [m for m in msgs if isinstance(m, dict)
             and m.get("role") in ("user", "assistant")
             and (m.get("content") or "").strip()]
    if not turns:
        return ""
    lines = [f"PREVIOUS SESSION {prior_id} (continuity — if the user's new ask "
             "contradicts this, the new statement supersedes it):"]
    for m in turns[-max(1, max_turns):]:
        role = (m.get("role") or "user").strip().upper()
        content = " ".join((m.get("content") or "").split())
        lines.append(f"{role}: {content}")
    return "\n".join(lines)[:max_chars]


__all__ = ["compact_session", "previous_session_brief"]
