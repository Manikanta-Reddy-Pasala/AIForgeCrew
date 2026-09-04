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


def _rich_preview(tool_name: str, args: dict) -> str:
    """The shared chat renderer's preview, or "" if it cannot produce one.

    It needs a cwd for on-disk diffs + fetching an item's current state; take it
    from the request context.
    """
    try:
        from aiforge_core.runtime import chat_agent, request_context
        cwd = (request_context.get_workspace_dir()
               or request_context.get_repo_root() or ".")
        return chat_agent._diff_preview(tool_name, args, cwd) or ""
    except Exception:  # noqa: BLE001 — caller falls back to the simple preview
        return ""


def _simple_preview(tool_name: str, args: dict) -> str:
    """Local fallback for the handful of tools this gate sees most."""
    from aiforge_core.runtime.diff_preview import unified_preview
    if tool_name in ("bash", "run_command", "run_shell", "shell"):
        return "$ " + str(args.get("cmd") or args.get("command") or "")
    if tool_name == "editor":
        body = args.get("file_text") or args.get("new_str") or ""
        path = args.get("path", "?")
        return (f"**editor {args.get('command', '')} `{path}`**\n\n```diff\n"
                + unified_preview(path, str(body), "") + "\n```")
    if tool_name in ("file_write", "file_create"):
        path = args.get("path", "?")
        return (f"**Write `{path}`**\n\n```diff\n"
                + unified_preview(path, str(args.get("content", "")), "") + "\n```")
    if tool_name == "file_patch":
        return (f"**Patch `{args.get('path', '?')}`**\n\n```diff\n"
                f"- {str(args.get('old_text', ''))[:1000]}\n"
                f"+ {str(args.get('new_text', ''))[:1000]}\n```")
    return ""


def _preview(tool_name: str, args: dict) -> str:
    """Human-readable preview of a tool call for the approval prompt.

    Content-AWARE by tool type: code → unified diff; confluence/jira/gitlab
    writes → formatted heading + fields + body (with a before/after diff for
    updates); commands → the shell line; everything else → pretty JSON. Shared
    with the simple/plan path so EVERY approval — simple, plan, or team — shows
    the same rich preview instead of a raw ``{"...": "..."}`` dump."""
    args = args or {}
    md = _rich_preview(tool_name, args)
    if md:
        return md
    try:
        simple = _simple_preview(tool_name, args)
    except Exception:  # noqa: BLE001
        simple = ""
    return simple or json.dumps(args, default=str)[:800]


def _force_review(name: str, args: dict, policy, sid) -> bool:
    """Gap D — pre-apply review mode: force human Approve/Reject for any
    file-mutating tool, even when policy would ALLOW it. Only when the session
    has it armed AND an interactive approver is attached (an autonomous run
    with no human still degrades to allow, at the ASK branch)."""
    return (policy != tool_policy.DENY
            and _is_mutating(name, args)
            and chat_approve.review_edits(sid)
            and chat_approve.has_emitter(sid))


def _emit_auto_approved(sid, name: str, scope) -> None:
    log.warning("tool_gate.auto_approved tool=%s flag=%s scope=%s",
                name, "commit_auto_approve", scope)
    try:
        if chat_approve.has_emitter(sid):
            chat_approve.emit(sid, {"type": "auto_approved", "name": name,
                                    "flag": "commit_auto_approve",
                                    "scope": scope})
    except Exception:  # noqa: BLE001 — the audit line already landed
        pass


def _auto_approved_commit(name: str, args: dict, sid) -> bool:
    """Captured-rule "never re-ask": an EXPLICITLY-enabled "commit directly"
    flag auto-approves a WHOLE-command git commit/add (``is_commit_command``
    rejects any chained/expanded command).

    An AUTONOMOUS run (sid None) ignores chat-set flags entirely (flag_active
    returns False), so it is never weakened here. The bypass is AUDITED, not
    invisible. PUSH is explicitly EXCLUDED — a push updates a remote (external,
    may trigger CI / a merge), so it ALWAYS requires an explicit approval even
    when local commits are auto-approved.

    Consulted REGARDLESS of the per-mode approval toggle. Gating it on
    ``not approvals_on`` made it dead code: the mode-OFF early return in the
    caller already allowed everything non-DENY, so the flag — and the UI pill
    that sets it — could never have an effect. The floors kept in the caller
    (DENY, forced review, push, chained) are what keep this safe.
    """
    try:
        from aiforge_core.runtime import rule_capture as _rc
        from aiforge_core.runtime import request_context as _reqctx
        cmd = args.get("cmd") or args.get("command") or ""
        if re.search(r"\bgit\s+push\b", cmd, re.I) or not _rc.is_commit_command(cmd):
            return False
        repo = _rc.repo_key(_reqctx.get_repo_root() or "")
        if not _rc.flag_active("commit_auto_approve", repo=repo, session_id=sid):
            return False
        _emit_auto_approved(sid, name, _rc.flag_active_scope(
            "commit_auto_approve", repo=repo, session_id=sid))
        return True
    except Exception:  # noqa: BLE001 — a broken flag store must not auto-approve
        return False


def _steer_from_rejection(sid, guidance: str) -> None:
    try:
        from aiforge_core.runtime import chat_interject
        # NOT a steer: this is a correction to the rejected action, not a new
        # instruction that may replace the whole request (chat_interject.push).
        chat_interject.push(sid, guidance, kind="reject")
    except Exception:  # noqa: BLE001 — best-effort steer
        pass


def _halt(sid, note: str) -> None:
    """Signal cancel so the pipeline stops at its next checkpoint
    (chat_pipeline/parallel_subtasks honour it); the user resumes by sending a
    new message. A 'cancelled' note (from /stop) is already a stop."""
    if note == "cancelled":
        return
    try:
        # NOTE: do NOT re-import chat_cancel here. A function-local import binds
        # the name as a local for the WHOLE function, so `chat_cancel.active()`
        # raised UnboundLocalError, which a broad except swallowed — silently
        # disabling the entire gate (DENY included).
        chat_cancel.cancel(sid)
    except Exception:  # noqa: BLE001 — best-effort halt
        pass


def _rejected_result(name: str, sid, decision: dict) -> dict:
    """What the tool returns when the human said no.

    Reject WITH guidance (typed in the approval card) → STEER + CONTINUE, same
    as the simple/plan loop: fold the guidance in as a steer so the Doer adjusts
    on its next model call, skip the rejected tool, and let the pipeline keep
    going (no halt). Only a bare reject (no guidance) HALTS the run.
    """
    from aiforge_core.runtime import chat_steer
    note = decision.get("note") or ""
    guidance = chat_steer.user_guidance(note)
    if guidance:
        _steer_from_rejection(sid, guidance)
        return {"ok": False, "rejected": True,
                "error": chat_steer.reject_directive(name, guidance)}
    _halt(sid, note)
    return {"ok": False, "rejected": True,
            "error": "user rejected this action"
                     + (f": {note}" if note else "")
                     + " — run halted; waiting for the user's next message."}


async def _ask_human(name: str, args: dict, sid, reason: str) -> dict | None:
    """Emit the approval card and BLOCK until /approve (another thread) resolves
    it. None when approved."""
    seq = chat_approve.request(sid)
    chat_approve.emit(sid, {"type": "approval", "id": seq, "name": name,
                            "args": args, "reason": reason,
                            "preview": _preview(name, args)})
    loop = asyncio.get_running_loop()
    decision = await loop.run_in_executor(None, chat_approve.wait, sid)
    if decision.get("decision") == "approve":
        return None
    return _rejected_result(name, sid, decision)


def _refuse_dangerous_unattended(verdict: dict) -> bool:
    """A DANGEROUS risk verdict with no approver attached is a refusal, unless
    the operator opted the whole install back in. Keyed on the classifier's
    verdict rather than the resulting policy, so an ``ask`` that came from
    something mild (an external write) still degrades to allow as before."""
    from aiforge_core.runtime.tools import command_risk
    if verdict.get("risk") != command_risk.DANGEROUS:
        return False
    return str(os.environ.get("AIFORGE_UNATTENDED_DANGEROUS", "")
               ).strip().lower() not in ("1", "true", "yes", "on")


async def _gate(name: str, args: dict) -> dict | None:
    """The policy decision for one tool call. None = let it run."""
    verdict = tool_policy.decide(name, args)
    policy = verdict["policy"]
    sid = chat_cancel.active()
    # Per-mode approval Settings toggle: approvals OFF for this run's chat mode
    # → never pause for human Approve/Reject. DENY still blocks (hard policy,
    # not a chat-mode approval).
    approvals_on = chat_approve.approvals_required(sid)
    forced = approvals_on and _force_review(name, args, policy, sid)

    if policy == tool_policy.ALLOW and not forced:
        return None
    if not approvals_on and policy != tool_policy.DENY:
        return None
    if policy != tool_policy.DENY and not forced \
            and _auto_approved_commit(name, args, sid):
        return None
    if policy == tool_policy.DENY:
        log.warning("tool_gate.deny tool=%s reason=%s", name, verdict["reason"])
        return {"ok": False, "blocked": "policy",
                "error": f"'{name}' is denied by policy: {verdict['reason']}"}
    # policy == ASK (or forced review) — need a human. Preserve autonomy.
    if not chat_approve.has_emitter(sid):
        # …but not for a DANGEROUS verdict. "Ask" degrading to "allow" is fine
        # for sudo or a global install; for `curl | sh`, a secret exfil, mkfs
        # or a fork bomb it means the gate reads the verdict and then runs the
        # command anyway, with nobody watching. The simple/plan loop already
        # hard-blocks these unattended (_autonomous_decision); the pipeline did
        # not, so the same command was refused in chat and executed by the
        # Doer. Same floor, both paths.
        if _refuse_dangerous_unattended(verdict):
            log.warning("tool_gate.unattended_dangerous tool=%s reason=%s",
                        name, verdict.get("reason"))
            return {"ok": False, "blocked": "risk",
                    "error": (f"'{name}' is refused: {verdict.get('reason')}. "
                              "This run has no human who could approve it. An "
                              "operator can set AIFORGE_UNATTENDED_DANGEROUS=1 "
                              "to allow dangerous commands in unattended runs."),
                    }
        return None            # autonomous run: no approver attached → allow
    reason = (verdict["reason"] if policy == tool_policy.ASK
              else "Review edits: confirm this file change before it lands.")
    return await _ask_human(name, args, sid, reason)


def make_approval_gate_callback():
    """ADK ``before_tool_callback`` enforcing policy/risk/approval.

    Disabled with ``AIFORGE_TOOL_GATE=0``. Returns None (no gate) when
    disabled so pipeline boot is unaffected."""
    if os.environ.get("AIFORGE_TOOL_GATE", "1") in {"0", "false", ""}:
        return None

    async def _cb(*, tool, args, tool_context, **_kw):
        try:
            return await _gate(getattr(tool, "name", "") or "", args or {})
        except Exception as exc:  # noqa: BLE001 — never block the pipeline on a gate bug
            # A bug in here silently DISABLES the gate, so make it loud
            # (this masked an UnboundLocalError that neutered the gate entirely).
            log.exception("tool_gate internal error (allow): %s", exc)
            return None

    return _cb


__all__ = ["make_approval_gate_callback"]
