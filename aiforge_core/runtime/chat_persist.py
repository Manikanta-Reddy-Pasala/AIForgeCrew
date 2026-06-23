"""Persist a finished chat turn — assistant message + auto-memory.

Shared by the simple-mode path (api ``_gen`` finally) and the team-mode
path (``chat_pipeline._drive`` finally). Team mode MUST persist from the
background driver — not the SSE generator — because the client can
disconnect mid-run: the driver still runs to completion and owns the real
final answer, whereas the generator's finally fires the instant the socket
closes with only a partial/empty result.
"""
from __future__ import annotations

import os


def persist_turn(*, session_id: int, cwd: str, prompt: str,
                 final_text: str, steps: list[dict], team: bool,
                 cancelled: bool, awaiting: bool) -> None:
    from aiforge_core.runtime import chat_store

    final_text = final_text or ""
    # Skip an empty, content-less turn (e.g. an immediate Stop before any
    # output) — don't leave a blank assistant bubble.
    if final_text.strip() or steps:
        chat_store.add_message(session_id, "assistant", final_text, steps)

    # Auto-memory: markdown note of what this turn did so the Memory tab +
    # ~/.aiforge/memory stay current. Best-effort. Skip trivial / cancelled /
    # question-only (awaiting) turns — they're not outcomes.
    if (os.environ.get("AIFORGE_CHAT_AUTO_MEMORY", "1") in ("0", "false")
            or not final_text or len(final_text.strip()) <= 40
            or awaiting or cancelled):
        return
    try:
        import datetime as _dt

        from aiforge_core.memory import md_store
        from aiforge_core.runtime.chat_agent import _repo_name
        tool_names = [s.get("name") for s in steps
                      if s.get("type") == "tool" and s.get("name")]
        section = (f"**Request:** {prompt[:300]}\n\n"
                   f"**Outcome:** {final_text[:1500]}\n\n"
                   + (f"**Tools used:** {', '.join(dict.fromkeys(tool_names))}\n"
                      if tool_names else ""))
        fresh = chat_store.get_session(session_id) or {}
        sess_title = (fresh.get("title") or "").strip()
        note_title = (sess_title if sess_title and sess_title != "New chat"
                      else (prompt.strip()[:80] or "chat session"))
        when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        md_store.upsert_section(
            source=f"chat-session:{session_id}", title=note_title,
            section_title=when, section_body=section, kind="session",
            tags=["chat", "team" if team else "simple"])
        repo = _repo_name(cwd)
        md_store.upsert_section(
            source=f"repo:{repo}", title=f"{repo} — project memory",
            section_title=f"{when} · {prompt.strip()[:50]}",
            section_body=f"{final_text[:600]}", kind="project",
            tags=["repo", repo])
    except Exception:  # noqa: BLE001
        pass


__all__ = ["persist_turn"]
