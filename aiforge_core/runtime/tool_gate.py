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

from aiforge_core.runtime import chat_approve, chat_cancel
from aiforge_core.runtime.tools import tool_policy

log = logging.getLogger("aiforge.tool_gate")

_MUTATING = {"editor", "file_write", "file_patch", "file_create"}


def _preview(tool_name: str, args: dict) -> str:
    try:
        if tool_name in ("bash", "run_command", "run_shell", "shell"):
            return "$ " + str(args.get("cmd") or args.get("command") or "")
        if tool_name == "editor":
            cmd = args.get("command", "")
            path = args.get("path", "?")
            body = args.get("file_text") or args.get("new_str") or ""
            return f"editor {cmd} {path}\n{str(body)[:1500]}"
        if tool_name in ("file_write", "file_create"):
            return (f"write {args.get('path', '?')}\n"
                    f"{str(args.get('content', ''))[:1500]}")
        if tool_name == "file_patch":
            return (f"patch {args.get('path', '?')}\n"
                    f"- {str(args.get('old_text', ''))[:400]}\n"
                    f"+ {str(args.get('new_text', ''))[:400]}")
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
            if policy == tool_policy.ALLOW:
                return None
            if policy == tool_policy.DENY:
                log.warning("tool_gate.deny tool=%s reason=%s", name, verdict["reason"])
                return {"ok": False, "blocked": "policy",
                        "error": f"'{name}' is denied by policy: {verdict['reason']}"}
            # policy == ASK — need a human. Preserve autonomy when none.
            sid = chat_cancel.active()
            if not chat_approve.has_emitter(sid):
                # autonomous run: no approver attached → don't hang, allow.
                return None
            seq = chat_approve.request(sid)
            chat_approve.emit(sid, {
                "type": "approval", "id": seq, "name": name,
                "args": args or {}, "reason": verdict["reason"],
                "preview": _preview(name, args or {}),
            })
            # Block off the event loop so /approve (another thread) can resolve.
            loop = asyncio.get_event_loop()
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
