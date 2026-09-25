"""Setting up a turn: completion function, step caps, writable roots, and
the loop state."""
from __future__ import annotations

import time
import types

from .._context import (
    _OUTPUT_REPEAT,
    _edit_claim_guard_enabled,
    _extension_budget,
    _safety_cap,
    _stuck_recovery_max,
    _text_of,
    _turn_deadline_s,
    _unattended_cap,
    _worktree_fingerprint,
)
from ._convo import (
    _build_convo,
)
from ._progress import progress_fields
from ._tasks import seed_board


def _resolve_complete_fn(complete_fn, role, mode="act", builder=""):
    """Resolve the completion fn: when the caller injected none, use the default
    and swap in native OpenAI tool-calling if the model/role supports it. Returns
    (complete_fn, native_on)."""
    _native_on = False
    if complete_fn is None:
        from aiforge_core.llm.client import complete as complete_fn  # type: ignore
        # Native OpenAI tool-calling — the reliable alternative to the text
        # ACTION/ARGS_JSON protocol that local models fumble into `ARGS_JSON: {}`
        # (the same mechanism OpenWebUI uses on these endpoints). When the model
        # supports it (probed once, or forced via AIFORGE_CHAT_TOOL_PROTOCOL),
        # swap in a completion that returns REAL structured args; the rest of the
        # loop is unchanged — it parses the SAME synthesized ACTION step. Only
        # when the caller didn't inject its own complete_fn (tests/doer paths).
        try:
            from .._native import make_native_complete_fn, native_tools_enabled
            if native_tools_enabled(role):
                complete_fn = make_native_complete_fn(
                    mode=mode or "act", builder=builder or "")
                _native_on = True
        except Exception:  # noqa: BLE001 — native must never break the turn
            pass

    return complete_fn, _native_on


#: Blocks the server appends to the user's message before the loop sees it.
_ADDED_BLOCKS = ("\n\n---\n[Interpreted request", "\n\n---\n[RESUME]",
                 "\n\n---\n[Deliverable", "\n\n---\n[Already read")


def _turn_goal(messages) -> str:
    """This turn's request: the last user message, without the enhancer's
    restatement."""
    from aiforge_core.runtime.chat_resume import quoted_request
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            text = _text_of(m).strip()
            quoted = quoted_request(text)       # "continue" + a resume brief
            if quoted:
                return quoted
            for marker in _ADDED_BLOCKS:
                text = text.split(marker)[0]
            return text.strip() or _text_of(m).strip()
    return ""


def _compute_caps(max_steps, session_id):
    """Compute the step-cap budget: the operator/default cap, a positive caller
    max_steps override, the unattended fallback, and the effective safety cap.
    Returns (cap_base, caller_cap, unattended, safety, capped)."""
    _cap_base = _safety_cap()
    # Only a POSITIVE max_steps is a caller's budget. 0 keeps its historical
    # meaning — "unset, use the default" — rather than becoming a one-step turn.
    _caller_cap = max_steps if isinstance(max_steps, int) and max_steps > 0 else None
    # 0 = NO step cap (Settings → Agent limits, or AIFORGE_CHAT_SAFETY_CAP=0).
    # The turn then ends the way a turn normally ends: the agent finishes, a
    # stall guard fires, the wall-clock deadline hits, or the user hits Stop.
    #
    # …but Stop is gated on a session id (see the cancel check in ``_loop._step_prologue``), so an
    # UNATTENDED run — the jobs scheduler, the analysis fan-out, the subtask
    # runners, text_doer — has no brake at all once the cap is off. "No limits"
    # is a promise to someone sitting in front of a chat; those runs keep a cap.
    _unattended = _cap_base <= 0 and _caller_cap is None and session_id is None
    if _unattended:
        _cap_base = _unattended_cap()
    safety = _caller_cap or _cap_base
    _capped = safety > 0
    return _cap_base, _caller_cap, _unattended, safety, _capped


def _writable_roots(messages, session_id) -> list:
    """Folders this chat may write to beyond its cwd: the ones the user named
    in their own turns (scope_guard.user_named_roots — `messages` is the real
    history; recall is injected as system blocks, not here), plus the ones the
    user allowed when the jail asked (chat_write_grants)."""
    try:
        from aiforge_core.runtime import scope_guard as _sg_roots
        roots = _sg_roots.user_named_roots(
            _text_of(m).split("\n\n---\n[Interpreted request")[0]
            for m in (messages or [])
            if isinstance(m, dict) and (m.get("role") or "user") == "user")
    except Exception:  # noqa: BLE001 — never break a turn over this
        roots = []
    try:
        from aiforge_core.runtime import chat_write_grants as _grants
        roots += [r for r in _grants.granted(session_id) if r not in roots]
    except Exception:  # noqa: BLE001
        pass
    return roots


def _build_loop_state(messages, cwd, role, max_steps, complete_fn,
                      session_id, mode, scope_globs, builder, strict_finish):
    """Assemble everything the ReAct loop needs (native detection, mode/read-only
    flags, scope allowlist, budget-capped convo, cap/deadline/extension budgets,
    the request meter and every per-turn counter) into one st namespace."""
    complete_fn, _native_on = _resolve_complete_fn(
        complete_fn, role, mode, builder)
    from aiforge_core.runtime import chat_cancel
    chat_cancel.set_active(session_id)
    _mode = (mode or "act").lower()
    plan_mode = _mode == "plan"
    analyze_mode = _mode == "analyze"
    # Both plan and analyze are READ-ONLY (same tool gate); they differ only in
    # the banner/output intent — plan produces a change-PLAN, analyze produces
    # FINDINGS. Used by the analysis fan-out's explore agents.
    readonly_mode = plan_mode or analyze_mode
    # Scope allowlist (autonomous Doer). When the caller passes globs, a
    # mutating file tool whose target path falls outside them is rejected
    # BEFORE it runs — the FunctionNode text Doer can't carry the native
    # scope_guard before_tool_callback, so this is its equivalent jail.
    # Empty/None = no restriction (back-compat; the chat UI passes nothing).
    _scope_globs = [g for g in (scope_globs or [])
                    if isinstance(g, str) and g]

    import collections
    # Caller-supplied max_steps (chat Quick mode, tests) is a DELIBERATE small
    # budget — honour it exactly and never auto-extend it. Only the
    # operator-level cap (Settings → env → default) is extendable, and only on
    # an interactive turn (see _ext_budget below).
    _cap_base, _caller_cap, _unattended, safety, _capped = _compute_caps(
        max_steps, session_id)
    # Wall-clock turn backstop. The 2000-step cap is not a real stopping
    # point on a slow local model — 2000 steps × seconds-to-minutes each is
    # effectively "forever" from the user's chair. This deadline bounds the
    # WHOLE turn regardless of step count, so a wandering or churning agent
    # (evades the exact-repeat stall guards in ``_action`` by varying its args) can't
    # run for hours. Generous default (1h) so it's a backstop, not a normal
    # limit; 0 disables. Set in Settings → Agent limits (or
    # AIFORGE_CHAT_TURN_DEADLINE_S). A step count is not a reason to stop:
    # with both knobs at 0 the turn runs until it finishes, the user hits
    # Stop, or the stall guard catches it repeating the same step.
    _turn_budget_s = _turn_deadline_s()
    _turn_deadline = (time.monotonic() + _turn_budget_s) if _turn_budget_s > 0 else None

    # Latest user message drives mentions (#4) + skill triggers (#6) +
    # memory recall. In simple/plan mode the API augments the last user turn
    # with an "[Interpreted request …]" enhancer block; key off the user's RAW
    # words (split that marker off) so recall/skills/mentions aren't diluted by
    # the boilerplate + restatement.
    _user_roots = _writable_roots(messages, session_id)

    _unlimited = not _capped and _turn_budget_s <= 0
    from ._approval import new_turn as _approvals_new_turn
    _approvals_new_turn(session_id)
    convo, _bundle, _asks, _dropped_playbooks = _build_convo(
        messages, cwd, role, readonly_mode=readonly_mode,
        plan_mode=plan_mode, analyze_mode=analyze_mode, builder=builder,
        strict_finish=strict_finish, session_id=session_id, native=_native_on,
        unlimited=_unlimited)
    from .._pause import inject as _inject_pause
    from .._pause import take as _take_pause
    _plan_asked = _inject_pause(convo, _take_pause(session_id))

    # OrderedDict, not dict: the prune in ``_action`` needs least-recently-SEEN order,
    # which only move_to_end can maintain (see its call site).
    action_counts: collections.OrderedDict[str, int] = collections.OrderedDict()
    recent_outputs: collections.deque = collections.deque(maxlen=_OUTPUT_REPEAT)
    condensed_notified = False
    continue_nudges = 0   # consecutive "narrated but didn't act" re-prompts
    stuck_recoveries = 0  # progress-recap nudges spent recovering a repeated step
    read_sigs_seen: set = set()   # read tool+args already executed this run
    # SEPARATE from read_sigs_seen, and never cleared: read_sigs_seen exists to
    # short-circuit a duplicate read while its RESULT is still in the window, so
    # a condense (which drops those results) must clear it. Progress is a
    # different question — "has this turn learned anything it did not know" —
    # and re-reading a file after a condense is not new knowledge. Conflating
    # the two handed a pure re-read loop the whole extension budget.
    # Bounded like action_counts, and for the same reason: an uncapped turn has
    # no step ceiling to hold it down. Losing the oldest entries only means an
    # ancient read can count as "new knowledge" a second time — the failure
    # direction that grants an extension, never one that hides a runaway.
    read_sigs_ever: collections.OrderedDict[str, bool] = collections.OrderedDict()
    _long_chain_help = _stuck_recovery_max() > 0   # 0 → full legacy behaviour

    # Mid-run steering (simple mode): let the user type WHILE the agent works —
    # each message is folded into the conversation as a live instruction the next
    # step must honour (parity with the pipeline's steering).
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_interject as _ci
            _ci.set_steerable(session_id, True)
        except Exception:  # noqa: BLE001
            pass


    # ── Budget extensions ────────────────────────────────────────────────
    # The step cap and the turn deadline are RUNAWAY guards, not task budgets.
    # A turn that is still producing NEW work earns another budget instead of
    # being killed with its work thrown away; a turn that is only spinning is
    # stopped exactly as before. Caller-set max_steps is never extended.
    # Only an INTERACTIVE turn extends. The unattended callers (text_doer,
    # parallel_subtasks, the analysis fan-out) pass no max_steps and have no
    # one watching — tripling their ceiling is spend nobody asked for. They
    # keep the old hard stop.
    # _extension_budget also bounds the PRODUCT (cap × (1+extensions), in steps
    # AND in wall clock) — each settings field validates in isolation, so the
    # multiplication is where an innocent-looking pair becomes a multi-day turn.
    _ext_budget = (_extension_budget(_cap_base, _turn_budget_s)
                   if (_caller_cap is None and session_id is not None) else 0)
    _extensions_used = 0
    _granted_at_step = -1     # step whose extension is already paid for
    # Progress is NEW WORK, not novel tool arguments: an agent that varies its
    # args mints a new action signature every step — that is the very churn the
    # deadline exists to stop, so counting distinct actions would hand every
    # extension to the runaway. Count what actually changes the world or the
    # agent's knowledge: file edits that landed, and reads of something not
    # read before.
    _reads_new = 0
    # Request meter: READ ONLY here. The turn boundary belongs to the route
    # (`_produce` in `api/routes/_chat/_producer.py`), which owns the whole
    # turn — including the enhancer and classifier calls that happen before
    # this loop, and team mode, which never enters this function at all.
    try:
        from aiforge_core.llm import call_meter as _meter
    except Exception:  # noqa: BLE001 — metering must never break a turn
        _meter = None
    _builder_nudged = False
    _builder_finalized = False
    _builder_final_tries = 0
    _multiask_checked = False   # one-time FINAL completeness gate (multi-ask)
    # Loop-engineering state (verify→fix on FINAL + progress gating). Only a
    # work-producing "act" run that actually EDITED files, with a real test
    # suite present, gets the verify gate — a Q&A turn (0 edits) is untouched.
    _edits_made = 0
    _verify_rounds = 0
    _verify_prev_fails = None   # last measured failure count (progress signal)
    _verify_stalls = 0
    # Claim-vs-reality guard: baseline the working tree ONCE so a final answer
    # claiming edits can be cross-checked against a real on-disk change (any
    # tool, not just the counted ones). "" = non-git workspace / no signal.
    # Skip the git call entirely when the guard is off.
    _wt_fp0 = _worktree_fingerprint(cwd) if _edit_claim_guard_enabled() else ""
    _edit_claim_nudges = 0
    # Progress mark at the last extension: (new reads, landed edits, worktree
    # fingerprint). Seeded with the turn's OWN baseline so an unchanged tree
    # does not read as a change on the first check.
    _progress_mark = (0, 0, _wt_fp0)



    st = types.SimpleNamespace(
        convo=convo, safety=safety, turn_deadline=_turn_deadline,
        condensed_notified=condensed_notified, continue_nudges=continue_nudges,
        stuck_recoveries=stuck_recoveries, extensions_used=_extensions_used,
        granted_at_step=_granted_at_step, reads_new=_reads_new,
        edits_made=_edits_made, progress_mark=_progress_mark,
        builder_nudged=_builder_nudged, builder_finalized=_builder_finalized,
        builder_final_tries=_builder_final_tries, multiask_checked=_multiask_checked,
        verify_rounds=_verify_rounds, verify_prev_fails=_verify_prev_fails,
        verify_stalls=_verify_stalls, edit_claim_nudges=_edit_claim_nudges,
        action_counts=action_counts, recent_outputs=recent_outputs,
        read_sigs_seen=read_sigs_seen, read_sigs_ever=read_sigs_ever,
        cap_base=_cap_base, ext_budget=_ext_budget, wt_fp0=_wt_fp0,
        turn_budget_s=_turn_budget_s, long_chain_help=_long_chain_help, cwd=cwd,
        capped=_capped, caller_cap=_caller_cap, unattended=_unattended,
        role=role, complete_fn=complete_fn, session_id=session_id,
        builder=builder, strict_finish=strict_finish, plan_mode=plan_mode,
        analyze_mode=analyze_mode, readonly_mode=readonly_mode,
        scope_globs=_scope_globs, asks=_asks, bundle=_bundle, meter=_meter,
        user_roots=_user_roots,
        dropped_playbooks=_dropped_playbooks, native_on=_native_on,
        pending_steps=[], batch_skipped=0, batch_mark=len(convo),
        batch_unread=False, early_reads={},
        board=seed_board(_asks, _turn_goal(messages)),
        board_used=False, plan_asked=_plan_asked, last_green_fp=None,
        board_nudges=0, board_closed_mark=None, unlimited=_unlimited,
        goal=_turn_goal(messages), steers=[],
        **progress_fields())
    return st
