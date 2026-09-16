"""The approval gate: auto-approval rules, command risk flags and the delete
guard, autonomous decisions, and rejections."""
from __future__ import annotations

import json
import re

from .._context import (
    _repo_name,
)
from .._preview import _diff_preview
from .._registry import (
    _is_mutating,
)
from ._shared import (
    _log,
)

_SHELL_TOOLS = ("run_command", "bash", "run_shell", "shell", "serve",
                "watch_until", "ui_check")


def _is_destructive_delete(cmd: str, cwd: str | None = None) -> bool:
    """Whether ``cmd`` deletes, unless the env opt-in already allows deletes."""
    try:
        from aiforge_core.runtime.tools import delete_guard
        return (not delete_guard.allow_delete(
            ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
            and delete_guard.is_destructive_delete(cmd, cwd))
    except Exception:  # noqa: BLE001
        return False


def _commit_auto_approved(cmd: str, repo: str, session_id) -> bool:
    """The scoped, revocable, audited "commit directly" opt-in.

    Consulted REGARDLESS of the per-mode approval toggle. Gating it on
    ``not _mode_approvals`` made it dead code: with approvals off nothing gates
    anyway, so the flag — and the UI pill that sets it — could never have an
    effect. It covers exactly one thing (a whole-command local git commit/add)
    and every floor below still gates: DENY, destructive delete, forced review,
    and any chained command (``is_commit_command`` rejects those). PUSH is
    excluded here as it is in tool_gate — it updates a remote, so it always
    asks.
    """
    try:
        from aiforge_core.runtime import rule_capture as _rc
        return bool(_rc.is_commit_command(cmd)
                    and not re.search(r"\bgit\s+push\b", cmd, re.I)
                    and _rc.flag_active("commit_auto_approve", repo=repo,
                                        session_id=session_id))
    except Exception:  # noqa: BLE001
        return False


def _delete_pre_confirmed(cmd: str, repo: str, session_id,
                          mode_approvals: bool) -> bool:
    """Whether a destructive delete is already confirmed without asking.

    Two ways, both requiring the mode's approvals to be OFF:

    * the captured per-repo/session ``allow_delete`` opt-in. It stays
      SUBORDINATE to the toggle — auto-confirming an ``rm -rf`` in a mode whose
      approvals are ON is a far bigger relaxation than skipping a commit
      prompt, and nothing asked for it.
    * approvals being off in an INTERACTIVE run (``session_id`` is not None),
      which is itself the confirmation. The delete floor used to ignore the
      toggle entirely, which made the toggle feel broken: the guard matches the
      whole command string, so routine remote maintenance —
      ``ssh host 'docker rm -f c'``, ``kubectl delete pod``, ``git clean -fdx``
      — kept prompting after approvals had been explicitly turned off.

    An AUTONOMOUS run can never reach the second: ``approvals_required(None)``
    is True by construction, so ``mode_approvals`` is True and both arms are
    closed. Unattended runs stay guarded.
    """
    if mode_approvals:
        return False
    try:
        from aiforge_core.runtime import rule_capture as _rc
        if _rc.flag_active("allow_delete", repo=repo, session_id=session_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    if session_id is not None:
        # Audited, not invisible — same rule as the commit bypass.
        _log.warning("chat.delete_auto_confirmed reason=approvals_off "
                     "session=%s cmd=%s", session_id, cmd[:200])
        return True
    return False


def _command_gate_flags(name, args, cwd, session_id, _mode_approvals):
    """For a shell-family tool, detect a destructive delete (unless it is
    already confirmed — see :func:`_delete_pre_confirmed`) and whether a scoped
    commit-auto-approve flag applies to a whole-command git commit/add. May set
    args[confirm_delete]. Returns ``(destructive_del, auto_commit)``."""
    if name not in _SHELL_TOOLS:
        return False, False
    cmd = args.get("cmd") or args.get("command") or ""
    repo = _repo_name(cwd)
    destructive_del = _is_destructive_delete(cmd, cwd)
    auto_commit = _commit_auto_approved(cmd, repo, session_id)
    if destructive_del and _delete_pre_confirmed(cmd, repo, session_id,
                                                 _mode_approvals):
        destructive_del = False
        args["confirm_delete"] = True
    return destructive_del, auto_commit


def _compute_gate_decision(name, args, cwd, session_id, verdict):
    """Decide whether one tool call must pause for approval: per-mode approval
    toggle, pre-apply forced review, destructive-delete detection, and the
    scoped commit-auto-approve / allow-delete opt-ins (each subordinate floor
    preserved; may set args[confirm_delete]). Returns
    ``(gate, destructive_del, force_review, bypass)`` with ``bypass`` =
    ``(auto_approved, scope)``."""
    from aiforge_core.runtime import chat_approve
    from aiforge_core.runtime.tools import tool_policy
    # Pre-apply review mode (Gap D): when armed for this session, force the
    # approval gate for any mutating tool even if policy would auto-allow.
    _force_review = (session_id is not None and _is_mutating(name, args)
                     and chat_approve.review_edits(session_id))
    # Per-mode approval Settings toggle (Chat/Plan/Pipeline). When ON, this
    # mode pauses for Approve/Reject AND the captured "never re-ask" bypass
    # flags below are IGNORED (the toggle is the master control — a user who
    # turned approvals ON wants to be asked, not silently auto-approved). When
    # OFF, ask-policy/review gates don't fire and the bypass flags apply.
    _mode_approvals = chat_approve.approvals_required(session_id)
    # Destructive delete (rm -rf, etc): the run_command tool has its OWN
    # confirm_delete arg gate (delete_guard). If we don't route it through
    # the approval gate AND mark it confirmed on approve, the tool keeps
    # refusing ("re-issue with confirm_delete=true") and the model loops
    # asking the user to "type yes" forever. So always gate it, and let the
    # human's Approve BE the confirmation.
    _destructive_del, _auto_commit = _command_gate_flags(
        name, args, cwd, session_id, _mode_approvals)
    # review_edits (_force_review) is an EXPLICIT per-request / global opt-in
    # ("hold my edits") — it must gate INDEPENDENTLY of the per-mode approval
    # toggle, else body.review_edits=True / AIFORGE_CHAT_REVIEW_EDITS=1 were
    # silently ignored whenever the (default-OFF) mode toggle was off. Only
    # the ASK-POLICY gate is subordinate to the mode toggle; forced review
    # and destructive deletes always gate.
    _gate = ((verdict["policy"] == tool_policy.ASK and _mode_approvals)
             or _force_review
             or _destructive_del)
    # A captured "commit directly" flag may auto-approve the gate ONLY when
    # the SOLE reason to gate is a pure whole-command git commit/add/push —
    # NEVER when a destructive delete (or any non-commit risk: forced review,
    # DENY) co-occurs. So `git commit && rm -rf` is NOT auto-approved.
    if _gate and _auto_commit and not _destructive_del and not _force_review \
            and verdict["policy"] != tool_policy.DENY:
        _gate = False
        # Audit: emit an attributable record of the bypass (not invisible).
        try:
            from aiforge_core.runtime import rule_capture as _rc2
            _ascope = _rc2.flag_active_scope(
                "commit_auto_approve", repo=_repo_name(cwd),
                session_id=session_id)
        except Exception:  # noqa: BLE001
            _ascope = None
        return _gate, _destructive_del, _force_review, (True, _ascope)
    return _gate, _destructive_del, _force_review, (False, None)


def _autonomous_decision(name, args, _destructive_del, verdict=None):
    """The approve/reject decision for an autonomous run (no human): auto-approve
    caution/review, hard-block only a DANGEROUS command or destructive delete.

    ``verdict`` is the policy decision already computed for this call. Its
    ``risk`` field covers what the name list below cannot: a notebook cell
    carries no command string, so `execute_ipython_cell` with `!curl x | sh`
    was auto-approved here while the identical string via bash was blocked."""
    # Autonomous path (parallel sub-Doer) — no human to approve.
    # Mirror run_shell's floor: auto-approve caution/review gates,
    # hard-block only truly DANGEROUS commands + destructive deletes
    # (a blanket reject here silently broke sudo / -g installs /
    # force-push in worktree-isolated autonomous runs).
    _danger = bool(_destructive_del)
    try:
        from aiforge_core.runtime.tools import command_risk as _cr
        if (verdict or {}).get("risk") == _cr.DANGEROUS:
            _danger = True
    except Exception:  # noqa: BLE001 — fall through to the name-list check
        pass
    if not _danger and name in ("run_command", "run_shell", "serve",
                                "bash", "shell", "watch_until", "ui_check"):
        try:
            from aiforge_core.runtime.tools import command_risk
            _lvl = command_risk.assess(
                args.get("cmd") or args.get("command") or "")["level"]
            _danger = _lvl == command_risk.DANGEROUS
        except Exception:  # noqa: BLE001
            _danger = False
    decision = ({"decision": "reject", "note": "autonomous: dangerous action blocked"}
                if _danger else
                {"decision": "approve", "note": "autonomous auto-approve"})
    return decision


#: chat_approve.wait's note when nobody answered.
_APPROVAL_TIMED_OUT = "approval timed out"


def _handle_rejection(name, args, session_id, convo, decision):
    """Handle a rejected/expired approval: reject-with-guidance folds the note in
    as a steer and continues; interactive reject-without-guidance stops and waits
    for the user; autonomous reject records an observation and continues. Returns
    "continue"/"return"."""
    from aiforge_core.runtime import chat_steer
    _rnote = decision.get("note") or ""
    if session_id is not None and _rnote == _APPROVAL_TIMED_OUT:
        # Nobody answered — the user may be away for hours. That is not a
        # "no": skip this call and let the run carry on with other work.
        result = {"ok": False, "approval_timed_out": True,
                  "error": f"`{name}` needs the user's approval, which did not "
                           "come in time. It did not run."}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        convo.append({"role": "user", "content":
                      f"OBSERVATION: {json.dumps(result)} Continue with any "
                      "work that does not need this call. If nothing else can "
                      "be done, finish with FINAL and say what is waiting for "
                      "approval."})
        return "continue"
    _user_guidance = chat_steer.user_guidance(_rnote)
    result = {"ok": False, "rejected": True,
              "error": "user rejected this action"
                       + (f": {_rnote}" if _rnote else "")}
    yield {"type": "tool", "name": name, "args": args, "result": result}
    # CHAT-ON-APPROVAL: if the user rejected WITH guidance, don't just
    # stop — fold the guidance in as a steer and CONTINUE so the agent
    # adjusts immediately (no separate follow-up message needed).
    if session_id is not None and _user_guidance:
        yield chat_steer.steer_event(_user_guidance)
        convo.append({"role": "user",
                      "content": chat_steer.reject_directive(
                          name, _user_guidance)})
        return "continue"
    # Interactive reject WITHOUT guidance is TERMINAL: STOP and WAIT
    # for the user (the old record-and-continue let a model that
    # didn't emit ASK: just keep going). Pause via awaiting_input;
    # the next user message resumes. Autonomous runs (session_id is
    # None) keep the record-and-continue behaviour.
    if session_id is not None:
        _ask = ("Stopped — you rejected the "
                f"`{name}` action"
                + ". Tell me what you'd like me to do instead, and "
                "I'll continue from there.")
        yield {"type": "message", "awaiting_input": True, "text": _ask}
        yield {"type": "done"}
        return "return"
    convo.append({"role": "user",
                  "content": f"OBSERVATION: {json.dumps(result)} "
                             "(the user rejected it — do NOT retry; "
                             "adjust or ASK what they want instead.)"})
    return "continue"

def _run_approval(name, args, cwd, session_id, convo, verdict, _destructive_del):
    """Surface the diff preview + Approve/Reject for a gated tool call and wait
    (autonomous runs auto-approve caution, hard-block only DANGEROUS/destructive;
    a Stop landing while the gate is open re-checks before dispatch). Mutates
    ``args``/``convo``. Returns "continue"/"return"/None."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    from aiforge_core.runtime.tools import tool_policy
    # Approval gate (#1): surface the action + diff preview, block on
    # the user's Approve/Reject (POST /api/chat/sessions/{id}/approve).
    preview = _diff_preview(name, args, cwd)
    seq = chat_approve.request(session_id) if session_id is not None else 0
    if verdict["policy"] == tool_policy.ASK:
        _reason = verdict["reason"]
    elif _destructive_del:
        _reason = "Confirm this destructive delete before it runs."
    else:
        _reason = "Review edits: confirm this file change before it lands."
    yield {"type": "approval", "id": seq, "name": name, "args": args,
           "reason": _reason, "preview": preview}
    if session_id is None:
        decision = _autonomous_decision(name, args, _destructive_del, verdict)
    else:
        decision = chat_approve.wait(session_id)
    # M4: a gate left unanswered (user navigated away) auto-rejects on
    # timeout — surface it explicitly so the UI shows "approval expired"
    # instead of silently moving on with a rejected action.
    if decision.get("note") == _APPROVAL_TIMED_OUT:
        yield {"type": "approval_expired", "id": seq, "name": name}
    if decision.get("decision") != "approve":
        return (yield from _handle_rejection(
            name, args, session_id, convo, decision))
    # Approved → the human's Accept IS the delete confirmation, so
    # satisfy the run_command tool's confirm_delete gate (otherwise it
    # re-refuses and the model loops asking the user again).
    if _destructive_del:
        args["confirm_delete"] = True
    # A Stop that landed WHILE the approval gate was open must not still
    # write the file — the file tools have no subprocess for cancel() to
    # kill, so re-check here before dispatching the (now-approved) tool.
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        yield {"type": "tool", "name": name, "args": args,
               "result": {"ok": False, "error": "cancelled"}}
        # continue (not break) → the top-of-loop cancel check emits the
        # accurate "stopped by user" rather than the safety-cap message.
        return "continue"
    return None


def _approval_gate(name, args, cwd, session_id, convo):
    """Permission + approval gate for one tool call. Returns "continue"/"return"
    to steer the caller's loop, or None to proceed to dispatch."""
    from aiforge_core.runtime.tools import tool_policy
    # Permission policy (#5) + risk (#7): allow / ask / deny.
    verdict = tool_policy.decide(name, args)
    if verdict["policy"] == tool_policy.DENY:
        result = {"ok": False, "blocked": "policy",
                  "error": f"'{name}' is denied by policy: {verdict['reason']}"}
        yield {"type": "tool", "name": name, "args": args, "result": result}
        from .._blocked import GUIDANCE
        kind = "egress" if "host_not_allowed" in str(verdict.get("reason")) else "policy"
        seen = {"next_step": GUIDANCE[kind], **result}
        convo.append({"role": "user",
                      "content": f"OBSERVATION: {json.dumps(seen)}"})
        return "continue"
    _gate, _destructive_del, _force_review, _bypass = _compute_gate_decision(
        name, args, cwd, session_id, verdict)
    if _bypass[0]:
        yield {"type": "auto_approved", "name": name,
               "flag": "commit_auto_approve", "scope": _bypass[1]}
    if _gate:
        _sig = yield from _run_approval(
            name, args, cwd, session_id, convo, verdict, _destructive_del)
        if _sig is not None:
            return _sig
    return None
