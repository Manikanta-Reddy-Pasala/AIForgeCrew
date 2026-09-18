"""Preparing a message turn: edits, slash commands, resume briefs, workspaces,
titles, checkpoints."""
from __future__ import annotations

import os

from ._core import (
    _af_log,
    _default_cwd,
)
from ._sessions import (
    _chat_workspace_root,
)


def _apply_edit_resend(session, session_id, body) -> None:
    """Edit-and-resend: restore the workspace to the edited turn's checkpoint
    (only for a session's OWN isolated scratch — a shared context dir or real
    repo is touched by others and a ``git restore --worktree`` would clobber
    their work) and truncate the conversation at that message. Fail-open."""
    from aiforge_core.runtime import chat_store
    if body.edit_from_message_id:
        try:
            _cwd_er = session.get("cwd") or _default_cwd()
            _sha = chat_store.message_checkpoint(session_id, body.edit_from_message_id)
            # Only roll the workspace back for a session's OWN isolated scratch.
            # A SHARED context dir (work/<kind>/<key>) or a real repo is touched
            # by other sessions/the operator; `git restore --worktree` there would
            # clobber their uncommitted work, and a checkpoint SHA taken in the
            # session's old repo doesn't even exist in a rebound one. Truncate
            # history either way; skip the destructive worktree restore.
            from aiforge_core.runtime import work_context as _wc0
            _own_scratch = (_wc0.context_for_path(_cwd_er) is None
                            and os.path.basename(os.path.normpath(_cwd_er))
                            .startswith("session-"))
            if _sha and _own_scratch:
                from aiforge_core.runtime import checkpoints as _ckpt
                _ckpt.restore(_cwd_er, _sha)
            chat_store.delete_messages_from(session_id, body.edit_from_message_id)
        except Exception as _exc:  # noqa: BLE001 — edit-resend must fail open
            _af_log.warning("edit-resend failed (session=%s msg=%s): %s",
                            session_id, body.edit_from_message_id, _exc)


def _expand_slash_command(session, body):
    """Expand a leading ``/<name> args`` that matches a LOCAL user command file
    into its markdown template (mutating ``body.content``); built-in /help is
    answered inline. Done here — before persist/title/fold — so one interception
    covers simple, plan AND team modes. Returns ``(expanded_name, help_text)``.
    Fail-open: any error → raw text."""
    _cmd_expanded: str | None = None
    _cmd_help_text: str | None = None
    try:
        from aiforge_core.runtime import commands as _commands
        _cmd_cwd = session.get("cwd") or _default_cwd()
        _cmd_exp = _commands.expand(body.content, _cmd_cwd)
        if _cmd_exp is not None:
            _cmd_name = body.content.strip()[1:].split(None, 1)[0]
            _known = _cmd_name in _commands.load(_cmd_cwd)
            if not _known and _commands.is_builtin(_cmd_name):
                _cmd_help_text = _cmd_exp          # /help — answered inline
            else:
                body.content = _cmd_exp            # replace with expanded template
                _cmd_expanded = _cmd_name
    except Exception as _cexc:  # noqa: BLE001 — expansion must never break a turn
        _af_log.debug("slash-command expand skipped: %s", _cexc)
    return _cmd_expanded, _cmd_help_text


def _apply_resume_brief(_rows, prompt, cwd, body, history) -> str:
    """Fold a resume inventory (what a stopped turn landed / left pending) into
    the last user row of ``history`` ONLY — never into ``prompt`` (the routers,
    trivial short-circuit, rule-capture and title generator all read prompt).
    Mutates ``history`` in place. Returns the brief (also used downstream by the
    single-agent path and the prelude notice). Fail-open."""
    _resume_brief = ""
    try:
        from aiforge_core.runtime import chat_resume as _resmod
        _resume_brief = _resmod.resume_preamble(
            _rows, prompt, cwd, forced=getattr(body, "resume", None))
    except Exception as _rexc:  # noqa: BLE001 — never break a turn over this
        _af_log.debug("resume brief skipped: %s", _rexc)
    if _resume_brief:
        # Into `history` ONLY — never into `prompt`. `prompt` is read by the
        # task/turn routers, the trivial-prompt short-circuit, the rule-capture
        # classifier, the title generator and the trace: prepending 4k of
        # inventory there would re-route the turn, spend an LLM classify on it,
        # and risk capturing "Rules for this run:" as a user preference. The
        # single-agent path sends `history`; the team pipeline gets the brief
        # explicitly at its call site (search: _resume_brief).
        for _hm in reversed(history):
            if _hm.get("role") == "user":
                _hm["content"] = f"{_hm.get('content') or ''}\n\n---\n{_resume_brief}"
                break
    return _resume_brief


def _rehome_context_workspace(cwd, prompt, session_id):
    """Re-home an EPHEMERAL session scratch (a session-<id> folder inside the
    managed chat-workspace root) onto the SHARED work/<kind>/<key>/ dir when the
    prompt names a durable context (Jira key, Confluence page), so that context's
    scratch persists across sessions. A pinned context or real repo is left as-is.
    Returns the (possibly rebound) cwd. Fail-open."""
    from aiforge_core.runtime import chat_store
    try:
        from aiforge_core.runtime import work_context as _wc
        # Ephemeral == the session's OWN scratch dir (a session-<id> folder INSIDE
        # the managed chat-workspace root) — NOT the configured default repo and
        # NOT a real repo the user pinned. Only such scratch is safe to re-home;
        # hijacking a real repo would strand the work in an empty folder.
        _ws_root = os.path.realpath(_chat_workspace_root())
        _cwd_real = os.path.realpath(cwd)
        _ephemeral = (
            _wc.context_for_path(cwd) is None
            and _cwd_real.startswith(_ws_root + os.sep)
            and os.path.basename(_cwd_real).startswith("session-"))
        if _ephemeral:
            _ctx = _wc.detect_context(prompt)
            if _ctx:
                cwd = _wc.context_dir(*_ctx)
                chat_store.set_session_cwd(session_id, cwd)
                _af_log.info("chat session %s bound to %s workspace %s",
                             session_id, _ctx[0], cwd)
    except Exception as _exc:  # noqa: BLE001 — never block a turn on this
        _af_log.debug("work-context bind skipped: %s", _exc)
    return cwd


def _apply_provisional_title(session_id, body, fresh_title) -> None:
    """Rename a still-unnamed session to a clean deterministic provisional
    title instantly (upgraded to a model-generated one after the turn).
    No-op unless the session is still unnamed. Fail-open to the raw first message."""
    if not fresh_title:
        return
    from aiforge_core.runtime import chat_store
    # Clean deterministic provisional (strips 'Build a…', trailing clauses,
    # Title-Cases) — reads well instantly; upgraded by the model title below
    # when that succeeds. Beats the raw truncated first message.
    try:
        from aiforge_core.runtime import chat_title as _ct
        _prov = _ct.provisional_title(body.content) or body.content.strip()[:60]
    except Exception:  # noqa: BLE001
        _prov = body.content.strip()[:60]
    chat_store.rename_session(session_id, _prov)


def _gen_title(prompt, session_id):
    try:
        from aiforge_core.runtime import chat_store as _cs
        from aiforge_core.runtime import chat_title
        # Titling is a ~20-token throwaway — route it to the cheap
        # 'triage' role so it doesn't contend with the main turn on a
        # serial local endpoint (was the big session role).
        _t = chat_title.suggest_title(prompt, role="triage")
        if _t:
            _cs.rename_session(session_id, _t)
    except Exception:  # noqa: BLE001 — titling must never break a run
        pass


def _auto_checkpoint(pc):
    from aiforge_core.runtime import chat_store
    # Snapshot the working dir at turn start so the user can roll back
    # this turn's edits. Best-effort; gated by env. Runs INSIDE _gen
    # (first, before streaming) so its git subprocesses don't delay the
    # StreamingResponse from opening.
    if os.environ.get("AIFORGE_CHAT_AUTO_CHECKPOINT", "1") in ("0", "false") \
            or pc.team:
        return
    try:
        import datetime as _dt

        from aiforge_core.runtime import checkpoints
        _snap = checkpoints.snapshot(
            pc.cwd, label=f"before: {pc.prompt[:50]}",
            when=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # Stamp this turn's snapshot onto the user message so edit-resend
        # can restore the workspace to exactly this turn's starting state.
        if isinstance(_snap, dict) and _snap.get("ok") and _snap.get("sha"):
            chat_store.set_message_checkpoint(pc._user_msg_id, _snap["sha"])
            # …and the turn's Changes diff starts from it: exactly what THIS
            # turn changed, not everything since HEAD (earlier turns, the
            # user's own uncommitted work).
            pc._checkpoint_sha = _snap["sha"]
    except Exception:  # noqa: BLE001
        pass


def _with_resume(pc, text):
    """Attach the resume brief to a PLANNER-facing prompt/spec.

    Every dispatch path plans from its own string — the enhanced spec, or
    the raw prompt for the analysis fan-out — and `history` reaches most of
    them as mere conversation context the planner is free to ignore. A
    brief that only rides in `history` is a resume that works in one mode.
    """
    return f"{text}\n\n---\n{pc._resume_brief}" if pc._resume_brief else text
