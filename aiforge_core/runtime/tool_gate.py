"""Universal tool-call gate — honors permission policy + risk + human
approval inside the ADK pipeline (team chat AND the autonomous Doer).

This is the pipeline-side counterpart to the inline gate in the simple
chat loop, so the approval feature is honored everywhere the Doer runs —
WITHOUT losing autonomy:

  * ``deny`` (operator policy) → always blocked, even autonomous.
  * ``ask``  → blocks for human Approve/Reject **only when an interactive
    approver is attached** (a chat session with a registered emitter).
    In an autonomous ticket run (no human, no emitter) ``ask`` degrades to
    allow — the run never hangs waiting for a click that can't come. The
    existing scope/delete/repeat guards remain the autonomous backstop.
  * ``allow`` → falls through (returns None) to the real tool.

Wired as an ADK ``before_tool_callback`` on the Doer. Returning a dict
short-circuits the tool with that dict as its result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from aiforge_core.runtime import chat_approve, chat_cancel
from aiforge_core.runtime.tools import tool_policy

log = logging.getLogger("aiforge.tool_gate")

# Canonical mutating tools AND the ADK Doer's edit aliases (doer_tools.py):
# write→file_write, patch/edit/str_replace→file_patch. The gate sees the alias
# NAME the model called, so the aliases must be listed here too or review-mode
# edits made via an alias would skip the human Approve/Reject gate.
_MUTATING = {"editor", "file_write", "file_patch", "file_create",
             "write", "patch", "edit", "str_replace"}

# The Anthropic ``editor`` tool multiplexes read + write sub-commands on one
# tool NAME (view/create/str_replace/insert/undo_edit). Only the WRITE
# sub-commands mutate — ``view`` (and any read/list) must NOT trip the
# review-edits gate.
_EDITOR_READONLY_CMDS = {"view", "read", "list", "ls", "cat", "open"}


def _is_mutating(name: str, args: dict | None) -> bool:
    """True when a tool call actually writes. For ``editor`` this depends on its
    ``command`` sub-command (view → read-only); every other mutating tool name
    always mutates."""
    if name not in _MUTATING:
        return False
    if name == "editor":
        cmd = str((args or {}).get("command")
                  or (args or {}).get("sub_command") or "").strip().lower()
        return cmd not in _EDITOR_READONLY_CMDS
    return True


def _preview(tool_name: str, args: dict) -> str:
    """Human-readable preview of a tool call for the approval prompt.

    Content-AWARE by tool type: code → unified diff; confluence/jira/gitlab
    writes → formatted heading + fields + body (with a before/after diff for
    updates); commands → the shell line; everything else → pretty JSON. Shared
    with the simple/plan path so EVERY approval — simple, plan, or team — shows
    the same rich preview instead of a raw ``{"...": "..."}`` dump."""
    # Reuse the rich renderer (handles confluence/jira/gitlab/diff/JSON). It
    # needs a cwd for on-disk diffs + fetching an item's current state; take it
    # from the request context. Falls back to the simple diffs below on any error.
    try:
        from aiforge_core.runtime import chat_agent, request_context
        cwd = (request_context.get_workspace_dir()
               or request_context.get_repo_root() or ".")
        md = chat_agent._diff_preview(tool_name, args or {}, cwd)
        if md:
            return md
    except Exception:  # noqa: BLE001 — fall through to the local simple preview
        pass
    from aiforge_core.runtime.diff_preview import unified_preview
    try:
        if tool_name in ("bash", "run_command", "run_shell", "shell"):
            return "$ " + str(args.get("cmd") or args.get("command") or "")
        if tool_name == "editor":
            cmd = args.get("command", "")
            path = args.get("path", "?")
            body = args.get("file_text") or args.get("new_str") or ""
            return (f"**editor {cmd} `{path}`**\n\n```diff\n"
                    + unified_preview(path, str(body), "") + "\n```")
        if tool_name in ("file_write", "file_create"):
            path = args.get("path", "?")
            return (f"**Write `{path}`**\n\n```diff\n"
                    + unified_preview(path, str(args.get("content", "")), "") + "\n```")
        if tool_name == "file_patch":
            return (f"**Patch `{args.get('path', '?')}`**\n\n```diff\n"
                    f"- {str(args.get('old_text', ''))[:1000]}\n"
                    f"+ {str(args.get('new_text', ''))[:1000]}\n```")
    except Exception:  # noqa: BLE001
        pass
    return json.dumps(args, default=str)[:800]


def make_approval_gate_callback():
    """ADK ``before_tool_callback`` enforcing policy/risk/approval.

    Disabled with ``AIFORGE_TOOL_GATE=0``. Returns None (no gate) when
    disabled so pipeline boot is unaffected."""
    if os.environ.get("AIFORGE_TOOL_GATE", "1") in {"0", "false", ""}:
        return None

    async def _cb(*, tool, args, tool_context, **_kw):
        try:
            name = getattr(tool, "name", "") or ""
            verdict = tool_policy.decide(name, args or {})
            policy = verdict["policy"]
            sid = chat_cancel.active()
            # Gap D — pre-apply review mode: force human Approve/Reject for any
            # file-mutating tool, even when policy would ALLOW it. Only when the
            # session has it armed AND an interactive approver is attached (an
            # autonomous run with no human still degrades to allow, below).
            force_review = (
                policy != tool_policy.DENY
                and _is_mutating(name, args or {})
                and chat_approve.review_edits(sid)
                and chat_approve.has_emitter(sid)
            )
            if policy == tool_policy.ALLOW and not force_review:
                return None
            # Captured-rule "never re-ask": an EXPLICITLY-enabled "commit
            # directly" flag auto-approves a WHOLE-command git commit/add
            # (is_commit_command rejects any chained/expanded command). Keep DENY
            # hard and never bypass a forced review. An AUTONOMOUS run (sid None)
            # ignores chat-set flags entirely (flag_active returns False), so it
            # is never weakened here. The bypass is AUDITED, not invisible.
            # PUSH is explicitly EXCLUDED — a push updates a remote (external,
            # may trigger CI / a merge), so it ALWAYS requires an explicit
            # approval even when local commits are auto-approved.
            if policy != tool_policy.DENY and not force_review:
                try:
                    from aiforge_core.runtime import rule_capture as _rc
                    _cmd = (args or {}).get("cmd") or (args or {}).get("command") or ""
                    from aiforge_core.runtime import request_context as _reqctx
                    _repo = _rc.repo_key(_reqctx.get_repo_root() or "")
                    _is_push = bool(re.search(r"\bgit\s+push\b", _cmd, re.I))
                    if _rc.is_commit_command(_cmd) and not _is_push \
                            and _rc.flag_active(
                            "commit_auto_approve", repo=_repo, session_id=sid):
                        _scope = _rc.flag_active_scope(
                            "commit_auto_approve", repo=_repo, session_id=sid)
                        log.warning(
                            "tool_gate.auto_approved tool=%s flag=%s scope=%s",
                            name, "commit_auto_approve", _scope)
                        try:
                            if chat_approve.has_emitter(sid):
                                chat_approve.emit(sid, {
                                    "type": "auto_approved", "name": name,
                                    "flag": "commit_auto_approve",
                                    "scope": _scope})
                        except Exception:  # noqa: BLE001
                            pass
                        return None
                except Exception:  # noqa: BLE001
                    pass
            if policy == tool_policy.DENY:
                log.warning("tool_gate.deny tool=%s reason=%s", name, verdict["reason"])
                return {"ok": False, "blocked": "policy",
                        "error": f"'{name}' is denied by policy: {verdict['reason']}"}
            # policy == ASK (or forced review) — need a human. Preserve autonomy.
            if not chat_approve.has_emitter(sid):
                # autonomous run: no approver attached → don't hang, allow.
                return None
            reason = (verdict["reason"] if policy == tool_policy.ASK
                      else "Review edits: confirm this file change before it lands.")
            seq = chat_approve.request(sid)
            chat_approve.emit(sid, {
                "type": "approval", "id": seq, "name": name,
                "args": args or {}, "reason": reason,
                "preview": _preview(name, args or {}),
            })
            # Block off the event loop so /approve (another thread) can resolve.
            loop = asyncio.get_running_loop()
            decision = await loop.run_in_executor(None, chat_approve.wait, sid)
            if decision.get("decision") != "approve":
                return {"ok": False, "rejected": True,
                        "error": "user rejected this action"
                                 + (f": {decision['note']}" if decision.get("note") else "")
                                 + " — do NOT retry; adjust or ask what they want."}
            return None
        except Exception as exc:  # noqa: BLE001 — never block the pipeline on a gate bug
            log.debug("tool_gate internal error (allow): %s", exc)
            return None

    return _cb


__all__ = ["make_approval_gate_callback"]
