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


def _is_real_repo(cwd: str | None) -> bool:
    """True only when ``cwd`` is inside a REAL git repo — not a managed work
    context (work/jira|confluence|web/<key>) and not a session scratch dir.
    Used to gate the per-repo project-memory unit so chat inside a ticket/page
    context doesn't create a cryptic ``compacted-<id>`` brief."""
    if not cwd:
        return False
    try:
        from aiforge_core.runtime import work_context
        if work_context.context_for_path(cwd) is not None:
            return False              # jira/confluence/web context folder
    except Exception:  # noqa: BLE001
        pass
    if os.path.basename(os.path.normpath(cwd)).startswith("session-"):
        return False                  # per-session scratch dir
    # walk up looking for a .git — cheap, no subprocess
    d = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _is_stop_event(step) -> bool:
    """The loop's OWN cancel event — ``{"type": "error", "text": "stopped by
    user"}`` — which is all a Stop press leaves behind when the route's
    cancelled flag did not survive the path (the fallback runner, a re-armed
    cancel). An agent QUOTING the same words lands in a tool RESULT, not in an
    error step, so this stays structural."""
    return (isinstance(step, dict) and step.get("type") == "error"
            and str(step.get("text") or "").strip().lower()
            .startswith("stopped by user"))


def _marked_steps(steps: list, *, awaiting: bool, cancelled: bool,
                  final_text: str) -> list:
    """Steps plus the structural markers a reload has to key on.

    ``add_message`` has no awaiting/stopped columns, so both are encoded as
    steps the frontend already recognises:

    - AWAITING: a turn that ended awaiting the user's reply (a clarify /
      stuck-loop pause) must carry that flag, or the reload reconcile loses it —
      the "answer below" banner and the composer's Reply mode both key off the
      stored message.
    - STOPPED: that a turn was stopped rather than finished otherwise lives only
      in local state. Matching the banner PROSE is both a false positive (an
      agent quoting "stopped by user", which is literally what run_command
      returns on cancel) and a false negative (a user Stop leaves no banner at
      all, just an error step).

    The stop marker is added only to a turn that is actually being WRITTEN: an
    immediate Stop with no output persists nothing, and a marker would have been
    "something" — resurrecting the empty turn.
    """
    if awaiting:
        steps = list(steps) + [{"type": "awaiting", "awaiting_input": True}]
    stopped = (final_text.strip() or steps) and (
        bool(cancelled)
        or final_text.strip().startswith("(stopped")
        or any(_is_stop_event(s) for s in (steps or [])))
    if stopped:
        steps = list(steps) + [{"type": "stopped",
                                "reason": "cancelled" if cancelled else "guard"}]
    return steps


def _append_trace(session_id: int, prompt: str, steps: list, final_text: str,
                  team: bool, cwd: str) -> None:
    """Reviewable on-disk trace: every action + response per message, per
    session, to ~/.aiforge/chat_traces/. Covers BOTH simple and team modes (both
    funnel through here). Runs for EVERY written turn — before the trivial-turn
    auto-memory skip — so the audit trail is complete. Best-effort."""
    try:
        from aiforge_core.runtime import chat_trace
        chat_trace.append_turn(session_id=session_id, prompt=prompt,
                               steps=steps, final_text=final_text, team=team,
                               cwd=cwd)
    except Exception:  # noqa: BLE001 — tracing must never break a turn
        pass


def _skip_auto_memory(final_text: str, awaiting: bool, cancelled: bool) -> bool:
    """Trivial / cancelled / question-only turns are not outcomes."""
    return bool(os.environ.get("AIFORGE_CHAT_AUTO_MEMORY", "1") in ("0", "false")
                or not final_text or len(final_text.strip()) <= 40
                or awaiting or cancelled)


def _note_title(chat_store, session_id: int, prompt: str) -> str:
    fresh = chat_store.get_session(session_id) or {}
    title = (fresh.get("title") or "").strip()
    if title and title != "New chat":
        return title
    return prompt.strip()[:80] or "chat session"


def _write_auto_memory(chat_store, session_id: int, cwd: str, prompt: str,
                       final_text: str, steps: list, team: bool) -> None:
    """Markdown note of what this turn did so the Memory tab + ~/.aiforge/memory
    stay current. Best-effort."""
    try:
        import datetime as _dt

        from aiforge_core.memory import md_store
        # The SAME repo key the RECALL path uses (_chat_repo_key = git-toplevel
        # basename). The old _repo_name (workspace-dir/subdir basename) filed
        # project memory under a DIFFERENT key than recall reads, so it was
        # written but never found.
        from aiforge_core.runtime.chat_agent import _chat_repo_key
        tool_names = [s.get("name") for s in steps
                      if s.get("type") == "tool" and s.get("name")]
        section = (f"**Request:** {prompt[:300]}\n\n"
                   f"**Outcome:** {final_text[:1500]}\n\n"
                   + (f"**Tools used:** {', '.join(dict.fromkeys(tool_names))}\n"
                      if tool_names else ""))
        when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        md_store.upsert_section(
            source=f"chat-session:{session_id}",
            title=_note_title(chat_store, session_id, prompt),
            section_title=when, section_body=section, kind="session",
            tags=["chat", "team" if team else "simple"])
        # PROJECT memory only for a REAL git repo. Chat run inside a jira/
        # confluence context folder (or a session scratch dir) has a "repo key"
        # that is actually a ticket/page-id / session-N — filing a repo: unit
        # there produced cryptic compacted-<id>.md briefs. Skipped; that
        # knowledge still reaches memory via the topic axis + the learner.
        if _is_real_repo(cwd):
            repo = _chat_repo_key(cwd)
            md_store.upsert_section(
                source=f"repo:{repo}", title=f"{repo} — project memory",
                section_title=f"{when} · {prompt.strip()[:50]}",
                section_body=f"{final_text[:600]}", kind="project",
                tags=["repo", repo])
    except Exception:  # noqa: BLE001
        pass


def persist_turn(*, session_id: int, cwd: str, prompt: str,
                 final_text: str, steps: list[dict], team: bool,
                 cancelled: bool, awaiting: bool,
                 mode: str = "simple", duration_s: "float | None" = None) -> None:
    from aiforge_core.runtime import chat_store

    final_text = final_text or ""
    steps = _marked_steps(steps, awaiting=awaiting, cancelled=cancelled,
                          final_text=final_text)
    # Skip an empty, content-less turn (e.g. an immediate Stop before any
    # output) — don't leave a blank assistant bubble.
    if not (final_text.strip() or steps):
        return
    chat_store.add_message(session_id, "assistant", final_text, steps,
                           mode=mode, duration_s=duration_s)
    _append_trace(session_id, prompt, steps, final_text, team, cwd)
    if _skip_auto_memory(final_text, awaiting, cancelled):
        return
    _write_auto_memory(chat_store, session_id, cwd, prompt, final_text, steps,
                       team)


__all__ = ["persist_turn"]
