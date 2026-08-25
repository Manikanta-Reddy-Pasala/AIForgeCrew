"""Per-session chat summaries → browsable md file + memory graph.

Chat sessions live only in SQLite. The :mod:`chat_learner` distils atomic
FACTS from each turn, but nothing keeps a concise, per-SESSION summary that a
human can ``cat`` or that ``unified_query`` can reach through the memory graph.

This module fills that gap. :func:`summarize_session` reads a session's
transcript, asks a cheap-tier LLM once for a short markdown summary, then
persists it to BOTH:

  * an md file — via :func:`aiforge_core.memory.md_store.upsert_section`, keyed
    by ``source=chat-session:<id>`` so one stable, human-browsable file per
    session is refreshed (not duplicated) as the chat grows; and
  * the configured memory backend — via
    :func:`aiforge_core.runtime.tools.memory_write.memory_write`, which routes
    to Neo4j when configured (the GRAPH) and no-ops to SQLite otherwise, so
    cross-session recall becomes graph-powered.

Lean + local: ONE cheap LLM call, a capped transcript and capped output,
boundary-gated by the caller (every N turns, not every turn). Soft-fail
EVERYWHERE — a summary is best-effort background work and must NEVER raise into
or slow a chat turn.

Env:
  AIFORGE_CHAT_SUMMARY=0              disable entirely (default on)
  AIFORGE_CHAT_SUMMARY_MAX_TOKENS    summary reply cap (default 400)
  AIFORGE_CHAT_SUMMARY_TIMEOUT_S     per-call timeout (default 60)
  AIFORGE_CHAT_SUMMARY_CHARS         transcript char cap (default 6000)
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.chat_summary")

_SUMMARY_SYS = (
    "You summarize a single chat session between an engineer and an AI "
    "assistant into a concise, factual markdown note that a teammate could "
    "skim later. Output ONLY markdown: a one-line topic, then 3-6 bullets "
    "covering what was discussed, decided, or left open. Keep concrete facts, "
    "decisions, file paths, commands, ids and numbers; drop pleasantries and "
    "repetition. If the chat has no durable content worth remembering, output "
    "NOTHING (an empty string)."
)


def _disabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SUMMARY", "1") in ("0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _transcript(messages: list[dict], limit: int) -> str:
    """Compact ``role: content`` transcript, keeping the LAST ``limit`` chars
    (the tail of a conversation carries the outcome/decisions)."""
    lines: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip() or "user"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role.upper()}: {content}")
    text = "\n\n".join(lines)
    if len(text) > limit:
        text = text[-limit:]
    return text


def _session_turns(chat_store, session_id) -> "tuple[list, str] | dict":
    """(user/assistant turns, session_title) for a session, or an error dict."""
    try:
        messages = chat_store.get_messages(session_id)
        session = chat_store.get_session(session_id) or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_summary chat_store failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    turns = [m for m in (messages or [])
             if isinstance(m, dict)
             and (m.get("role") in ("user", "assistant"))
             and (m.get("content") or "").strip()]
    title = (session.get("title") or "").strip() or f"session {session_id}"
    return turns, title


def _generate_summary(_llm, turns, session_title) -> "str | dict":
    """One cheap-tier LLM call over a capped transcript → the cleaned summary
    text, or an error dict on failure."""
    transcript = _transcript(turns, _int_env("AIFORGE_CHAT_SUMMARY_CHARS", 6000))
    messages_llm = [
        {"role": "system", "content": _SUMMARY_SYS},
        {"role": "user", "content":
            f"Summarize this chat session (title: {session_title}).\n\n"
            + transcript}]
    try:
        raw = _llm.complete(
            "learner", messages_llm,
            max_tokens=_int_env("AIFORGE_CHAT_SUMMARY_MAX_TOKENS", 400),
            temperature=0.0,
            timeout_s=_int_env("AIFORGE_CHAT_SUMMARY_TIMEOUT_S", 60))
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_summary llm failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    summary = (raw or "").strip()
    if summary.startswith("```"):        # drop an accidental wrapping ``` fence
        import re
        summary = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", summary)
        summary = re.sub(r"\n?```\s*$", "", summary).strip()
    return summary


def _persist_summary(md_store, _mw, session_id, session_title, summary: str,
                     repo: str, tags: list) -> "tuple[int, list[str]]":
    """Write the summary to (a) a browsable md file + local memory and (b) the
    memory graph. Returns ``(written, errors)`` — each store soft-fails."""
    written, errors = 0, []
    try:
        md_store.upsert_section(
            source=f"chat-session:{session_id}",
            title=f"Chat session {session_id}: {session_title}",
            section_title="Summary", section_body=summary,
            kind="chat_summary", tags=tags, repo=repo)
        written += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_summary md_store failed: %s", exc)
        errors.append(f"md_store: {exc}")
    try:
        _mw.memory_write(
            text=f"CHAT SESSION {session_id} ({session_title}): {summary}",
            kind="chat_summary", tags=tags, repo=repo)
        written += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_summary memory_write failed: %s", exc)
        errors.append(f"memory_write: {exc}")
    return written, errors


def summarize_session(session_id, repo: str, *, min_turns: int = 4) -> dict:
    """Summarize ONE chat session and persist it to md + the memory graph.

    Returns a result dict, NEVER raises:
      * ``{"ok": False, "skipped": "disabled"}``      kill switch off
      * ``{"ok": True,  "skipped": "too_short"}``      < ``min_turns`` messages
      * ``{"ok": True,  "written": 0}``                LLM produced nothing
      * ``{"ok": True,  "written": N, ...}``           persisted to md + graph
      * ``{"ok": False, "error": ...}``                any failure (soft-fail)
    """
    if _disabled():
        return {"ok": False, "skipped": "disabled"}
    repo = repo or os.environ.get("AIFORGE_AFM_REPO", "") or "repo"
    try:
        from aiforge_core.llm import client as _llm
        from aiforge_core.memory import md_store
        from aiforge_core.runtime import chat_store
        from aiforge_core.runtime.tools import memory_write as _mw
    except Exception as exc:  # noqa: BLE001
        log.debug("chat_summary import failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    got = _session_turns(chat_store, session_id)
    if isinstance(got, dict):
        return got
    turns, session_title = got
    if len(turns) < max(1, int(min_turns)):
        return {"ok": True, "skipped": "too_short"}

    summary = _generate_summary(_llm, turns, session_title)
    if isinstance(summary, dict):
        return summary
    if not summary:
        return {"ok": True, "written": 0}

    tags = ["chat", "session", f"session:{session_id}"]
    written, errors = _persist_summary(md_store, _mw, session_id, session_title,
                                       summary, repo, tags)
    if errors and written == 0:
        return {"ok": False, "error": "; ".join(errors)}
    if errors:
        return {"ok": False, "written": written, "error": "; ".join(errors)}
    log.info("chat_summary: session=%s repo=%s written=%d", session_id, repo, written)
    return {"ok": True, "written": written}


__all__ = ["summarize_session"]
