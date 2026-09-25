"""The chat ReAct loop's driver: one step at a time until the turn ends.

Each job it drives lives in ``chat_agent/_turn/``; this module re-exports every
name it used to define (not the names it only imported), so ``_loop.<name>``
keeps working. Patch a helper in the ``_turn`` module that calls it.
"""
from __future__ import annotations

import os
import time  # noqa: F401  # tests patch time.sleep through this module
from collections.abc import Callable, Iterator

from ._prompt import _parse
from ._registry import TOOLS  # noqa: F401  # re-exported
from ._shell import _READ_OBS_TOOLS
# Everything the single module defined stays importable from here.
from ._turn._action import (  # noqa: F401
    _action_stall_guard,
    _ask_write_grant,
    _post_tool,
    _pre_dispatch_gates,
    _pre_tool_checks,
    _record_edit,
    _record_read,
    _workspace_jail,
)
from ._turn._approval import (  # noqa: F401
    _SHELL_TOOLS,
    _approval_gate,
    _autonomous_decision,
    _command_gate_flags,
    _commit_auto_approved,
    _compute_gate_decision,
    _delete_pre_confirmed,
    _handle_rejection,
    _is_destructive_delete,
    _run_approval,
)
from ._turn._batch import (  # noqa: F401
    _NOT_BATCHABLE,
    _batch_stop_reason,
    _call_sig,
    _cancel_early_reads,
    _drop_batch,
    _pop_queued_step,
    _queue_batched_reads,
    _rebase_batch,
    _take_early_read,
    _unread_batch_msgs,
)
from ._turn._blocks import (  # noqa: F401
    _append_context_blocks,
    _append_learning_recall,
    _append_recall_blocks,
    _append_session_blocks,
    _prepend_priority_blocks,
)
from ._turn._completion import (  # noqa: F401
    _RETRY_STOP,
    _STEERED,
    _emit_completion_failure,
    _max_gen_per_step,
    _retry_completion,
    _retry_plan,
    _run_completion,
)
from ._turn._convo import (  # noqa: F401
    _append_directive,
    _build_convo,
    _codegraph_directive,
    _history_to_convo,
    _sandbox_directive,
    _seed_prompt,
)
from ._turn._finish import (  # noqa: F401
    _claim_guard,
    _emit_suggestion,
    _final_nudges,
    _handle_continue_step,
    _handle_final,
    _is_clean_tree,
    _last_user_message,
    _predict_next_step,
    _turn_summary,
    _verify_on_final,
)
from ._turn._limits import (  # noqa: F401
    _builder_nudge,
    _cap_stop_reason,
    _condense_and_report,
    _deadline_guard,
    _drain_steering,
    _may_extend,
    _step_cap_guard,
    _stuck_output_guard,
)

from ._turn._shared import (  # noqa: F401
    _ACTION_SIG_MAX,
    _THE_FINALIZE_TOOL,
    _log,
)
from ._turn._state import (  # noqa: F401
    _build_loop_state,
    _compute_caps,
    _resolve_complete_fn,
    _writable_roots,
)
from ._turn._tool_dispatch import (  # noqa: F401
    _REMOVED_SEARCH_TOOLS,
    _SECRET_ARGS,
    _dispatch_tool,
    _invoke_tool,
    _shown_args,
    _unknown_tool_result,
)


def _emit_loop_prelude(st):
    """Emit the one-time turn prelude events: a dropped-playbook warning, the
    multi-ask subtasks dock, and the native-tool-calling banner."""
    if st.dropped_playbooks:
        yield {"type": "thought", "role": "system",
               "text": "⚠ context window too small — dropped the "
                       + " + ".join(st.dropped_playbooks) + " block(s): matched "
                       "workflows/skills may NOT be followed this turn. Load "
                       "the model at a larger context window to fix this."}
    if st.asks:
        yield {"type": "subtasks", "items": [
            # `goal` is the field name every other producer uses and the one
            # the UI's Tasks panel reads. This path emitted `title` alone, so
            # expanding that panel on a split-asks turn threw "Cannot read
            # properties of undefined (reading 'length')" and the view died.
            # Both keys go out: `title` is kept for anything already reading it.
            {"slug": f"part-{i + 1}", "goal": a, "title": a, "status": "pending"}
            for i, a in enumerate(st.asks)]}
    # One-time visibility: confirm native tool-calling is driving this run (every
    # tool call goes through native OpenAI function-calling, not the text
    # ACTION/ARGS_JSON protocol). Opt out of the banner: AIFORGE_CHAT_NATIVE_BANNER=0.
    if st.native_on and os.environ.get("AIFORGE_CHAT_NATIVE_BANNER", "1") not in ("0", "false"):
        try:
            from ._catalog_gate import gate_schemas
            from ._tools._schemas import NATIVE_TOOL_SCHEMAS, filter_native
            _mode = ("plan" if st.plan_mode else
                     "analyze" if st.analyze_mode else "act")
            _ntools = len(filter_native(
                gate_schemas(NATIVE_TOOL_SCHEMAS), mode=_mode,
                text=getattr(st, "goal", "") or ""))
        except Exception:  # noqa: BLE001
            _ntools = 0
        yield {"type": "thought", "role": "system",
               "text": f"🔌 native tool-calling active ({_ntools} tools)"}


def _step_prologue(st, n, _cwd, role, complete_fn, session_id, builder):
    """Per-step prologue up to a parsed reply: cap/deadline/cancel guards, builder
    nudge, steering drain, condense+usage, the model completion, and the stuck-
    output guard. Returns ``(out, signal)`` with signal 'return'/'continue'/None."""
    from aiforge_core.runtime import chat_cancel
    _sig = yield from _step_cap_guard(st, n)
    if _sig == "return":
        return None, "return"
    # The cancel token covers Stop on a live turn. A scheduled agent also
    # binds a stop event so a later "drop that" can halt it without
    # replacing a token a different turn already owns.
    try:
        from aiforge_core.runtime.run_interrupt import reason as _interrupt_reason
        _job_stopped = _interrupt_reason(session_id) == "stop"
    except Exception:  # noqa: BLE001
        _job_stopped = False
    if _job_stopped or (
            session_id is not None and chat_cancel.is_cancelled(session_id)):
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}
        return None, "return"
    _builder_nudge(st, builder, n)
    _sig = yield from _deadline_guard(st, n)
    if _sig == "return":
        return None, "return"
    yield from _drain_steering(st, session_id)
    yield from _condense_and_report(st, role, complete_fn, session_id, st.meter)
    out = yield from _run_completion(st, role, complete_fn, session_id, st.meter)
    st.batch_unread = False        # the model has now read the last batch
    if out is _RETRY_STOP:
        return None, "return"
    if out is _STEERED:
        # A message arrived during a retry/outage wait. Drain it next step
        # and let the model decide; do not spend this step on the old call.
        return None, "continue"
    _sig = yield from _stuck_output_guard(st, out)
    if _sig == "return":
        return None, "return"
    if _sig == "continue":
        return None, "continue"
    return out, None


def _run_action_path(st, step, n, cwd, session_id):
    """The action path for a tool step: stall guard, plan/approval/hook/scope
    gates, tool dispatch and post-tool bookkeeping. Returns return/continue/None."""
    # action
    name = step["tool"]
    # Coerce to a dict: a model can emit `ARGS_JSON: null` (or a JSON scalar)
    # which parses to None/non-dict; every tool does `args.get(...)` and would
    # crash. An empty dict lets the tool return its own instructive error.
    args = step["args"] if isinstance(step["args"], dict) else {}
    sig = _call_sig(name, args)
    _sig = yield from _gated_action(st, step, name, args, sig, n, cwd, session_id)
    if _sig in ("repeat", "handled"):
        return "continue"
    if _sig == "continue":
        # A gate refused or redirected this call: the rest of the batch waits
        # for the model to read why.
        _drop_batch(st, "an earlier call was blocked")
    return _sig


def _gated_action(st, step, name, args, sig, n, cwd, session_id):
    """Stall guard, gates, dispatch and bookkeeping for one tool call. Returns
    return/continue/None, "repeat" when a read already done was skipped, or
    "handled" when the loop did the call's bookkeeping itself."""
    repeat = bool(st.long_chain_help and name in _READ_OBS_TOOLS
                  and sig in st.read_sigs_seen)
    _sig = yield from _action_stall_guard(st, name, args, sig, st.long_chain_help)
    if _sig == "return":
        return "return"
    if _sig == "continue":
        return "repeat" if repeat else "continue"
    if step.get("thought"):
        yield {"type": "thought", "text": step["thought"]}
    _sig = yield from _pre_dispatch_gates(st, name, args, st.readonly_mode,
                                          st.analyze_mode)
    if _sig in ("continue", "handled"):
        return _sig
    _sig = yield from _approval_gate(name, args, cwd, session_id, st.convo)
    if _sig == "return":
        return "return"
    if _sig == "continue":
        return "continue"
    _hb = yield from _pre_tool_checks(st, name, args, cwd, st.scope_globs)
    if _hb in ("continue", "return"):
        return _hb
    result = yield from _dispatch_tool(name, args, cwd, n, _hb,
                                       _take_early_read(st, sig))
    yield from _post_tool(st, name, args, result, cwd, sig, n,
                          st.long_chain_help, st.bundle)
    return None


def _dispatch_step(st, out, n, cwd, role, _complete_fn, session_id, builder,
                   strict_finish):
    """Process one parsed reply: FINAL / ask / continue handling, then the action
    path (stall guard, plan/approval/hook/scope gates, tool dispatch, post-tool
    bookkeeping). Returns 'return'/'continue'/None."""
    st.convo.append({"role": "assistant", "content": out})
    # A direction arrived while this reply was being written. Do not start
    # the tool, end the turn, or schedule the task from a reply that has
    # not seen it. The next step folds the message in and the model decides.
    if session_id is not None:
        from aiforge_core.runtime import chat_interject
        if chat_interject.pending(session_id):
            return "continue"
    step = _parse(out)
    if step["kind"] == "final":
        _sig = yield from _handle_final(
            st, step, builder, strict_finish, st.plan_mode, st.readonly_mode,
            cwd, st.asks, st.wt_fp0)
        if _sig == "return":
            return "return"
        if _sig == "continue":
            return "continue"
    if step["kind"] == "ask":
        # Agent is asking the user a question — show it + wait for the next
        # message (which answers it). awaiting_input flags the UI.
        yield {"type": "message", "awaiting_input": True, "text": step["text"]}
        yield {"type": "done"}
        return "return"
    if step["kind"] == "continue":
        _sig = yield from _handle_continue_step(st, step, builder, cwd)
        if _sig == "return":
            return "return"
        if _sig == "continue":
            return "continue"
    st.continue_nudges = 0   # a real action resets the narration guard
    return (yield from _run_action_path(st, step, n, cwd, session_id))


def run_chat_agent(
    messages: list[dict], *,
    cwd: str,
    role: str = "doer",
    max_steps: int | None = None,   # kept for callers/tests; None = no cap
    complete_fn: Callable[..., str] | None = None,
    session_id: int | None = None,
    mode: str = "act",              # "act" = full tools; "plan" = read-only
    scope_globs: list[str] | None = None,  # autonomous Doer scope allowlist
    builder: str | None = None,     # job|skill|workflow|rule — task charter
    strict_finish: bool = False,    # work-producing run (doer): an IMPLICIT
    #                                 bare-prose final is premature narration →
    #                                 nudge to act, don't quit with no work done
) -> Iterator[dict]:
    """Drive the ReAct loop until the agent finishes or a stuck loop is
    detected (NOT a step count). Yields SSE-ready event dicts:

    ``{"type": "thought", "text"}`` · ``{"type": "tool", "name", "args",
    "result"}`` · ``{"type": "message", "text"}`` (final) ·
    ``{"type": "approval", ...}`` (ask-policy gate) ·
    ``{"type": "error", "text"}`` · ``{"type": "done"}``.
    """
    st = _build_loop_state(
        messages, cwd, role, max_steps, complete_fn, session_id, mode,
        scope_globs, builder, strict_finish)
    # _build_loop_state RESOLVES the completion fn (injects native tool-calling
    # when the caller passed none, as chat does) into st.complete_fn. The loop
    # below still threads a `complete_fn` local into _step_prologue/_run_completion
    # — rebind it to the resolved one, or native chat calls None(role, convo)
    # ("'NoneType' object is not callable" → the "model didn't respond" retry
    # loop on a model that answered fine). Regression from the run_chat_agent
    # decomposition: the resolve moved into the helper but the local kept the
    # caller's original None.
    complete_fn = st.complete_fn
    n = 0
    yield from _emit_loop_prelude(st)
    try:
        while True:
            n += 1
            # A batch of reads from the last reply runs without asking the model
            # again; each still passes every gate in _dispatch_step.
            out = _pop_queued_step(st, n, session_id)
            if out is None:
                out, _sig = yield from _step_prologue(
                    st, n, cwd, role, complete_fn, session_id, builder)
                if _sig == "return":
                    return
                if _sig == "continue":
                    continue
                _queue_batched_reads(st, n)
            _sig = yield from _dispatch_step(
                st, out, n, cwd, role, complete_fn, session_id, builder, strict_finish)
            if _sig == "return":
                return
            if _sig == "continue":
                continue
    finally:
        # However the turn ends (answer, a pause for the user, a closed
        # stream), batched reads still waiting for a worker never run.
        _cancel_early_reads(st)
