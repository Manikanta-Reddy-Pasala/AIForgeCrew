"""The team run's turn bookkeeping: its subtask panel, session binding, queue
tail, final answer, and the persisted turn when it stops early or falls back."""
from __future__ import annotations

import os
import queue
import time

from .chat_pipeline_events import (  # noqa: F401  # re-exported
    _enhancer_block_reason,
    _event_text,
    _fold_team_event,
    _guard_edit_claim,
    _part_events,
    _planner_subtask_event,
    _process_team_event,
    _team_change_events,
    _team_streaming,
    map_event,
    partial_events,
)
from .chat_pipeline_prompt import (  # noqa: F401  # re-exported
    _build_team_prompt,
    _history_preamble,
)


def _dur(started_at: "float | None") -> "float | None":
    """Per-turn wall-clock seconds since ``started_at`` (None → unknown)."""
    return round(time.time() - started_at, 2) if started_at else None

_SENTINEL = object()


def _finalize_subtasks(items: list[dict] | None, run_ok: bool,
                       cancelled: bool) -> list[dict]:
    """Reconcile the Planner's subtask panel to the run outcome.

    The chat (sequential team) pipeline shows a Planner-decomposed task
    list but the Doer executes it in one pass — there's no per-subtask
    completion signal, so without this the panel sits at "0/N pending"
    after the run reports complete. Mutates each item's status in place
    (the same dicts are persisted in ``steps`` → reload agrees) and
    returns the matching ``subtask_update`` events to stream live.

    done on a clean finish; failed on error / user-stop.
    """
    if not items:
        return []
    status = "done" if (run_ok and not cancelled) else "failed"
    out: list[dict] = []
    for it in items:
        it["status"] = status
        out.append({"type": "subtask_update",
                    "slug": it.get("slug"), "status": status})
    return out


async def _team_final_state(svc, session) -> dict:
    """The ADK session state at run end, {} on any error."""
    try:
        sess = await svc.get_session(app_name="aiforge-chat", user_id="chat",
                                     session_id=session.id)
        return dict(sess.state or {})
    except Exception:
        return {}


def _promote_team_answer(by_role: dict, st: dict, final: str,
                         enhancer_blocked) -> str:
    """The conversational answer. Learner/validator/refiner emit JSON verdicts,
    not prose, and run AFTER the Doer — so never let them win. ``doer_outcome``
    is the key the Doer actually writes (native + the local text_doer
    FunctionNode, which emits no ADK "doer"-authored events, so on a LOCAL
    endpoint the answer used to fall through to the Researcher or a bare
    "Done.")."""
    if enhancer_blocked:
        return (f"I need more detail before I can build this — {enhancer_blocked}. "
                f"Could you say what to build/change and where?")
    return (by_role.get("doer") or st.get("doer_outcome")
            or by_role.get("researcher") or final or "Done.")


async def _compute_team_answer(svc, session, by_role, final, enhancer_blocked,
                               cwd, seq_start_sha) -> "tuple[str, list]":
    """The final answer text + change events for a team run. The Changes diff is
    computed BEFORE surfacing the answer so the claim-vs-reality guard can
    cross-check an "applied fixes" claim against the ACTUAL diff."""
    st = await _team_final_state(svc, session)
    msg = _promote_team_answer(by_role, st, final, enhancer_blocked)
    change_events = _team_change_events(cwd, seq_start_sha, enhancer_blocked)
    msg = _guard_edit_claim(msg, cwd, seq_start_sha, enhancer_blocked,
                            change_events)
    return msg, change_events


def _bind_team_session(session_id, q) -> None:
    """Bind this driver thread to ``session_id`` so Stop can cancel it, attach an
    interactive approver + mark the run steerable, and expose the session to the
    Doer's subtask_update tool + the request meter (env AND thread contextvar —
    the env var is process-global and never cleared, so it can't be trusted for
    metering; the driver runs in a bare Thread that inherits no context)."""
    from aiforge_core.runtime import chat_cancel
    chat_cancel.set_active(session_id)
    if session_id is None:
        return
    from aiforge_core.runtime import chat_approve, chat_interject
    from aiforge_core.runtime import request_context as _rc
    chat_approve.set_emitter(session_id, q.put)
    chat_interject.set_steerable(session_id, True)
    os.environ["AIFORGE_CURRENT_SESSION"] = str(session_id)
    _rc.set_session_id(session_id)


def _persist_stop_before_start(session_id, cwd, raw_prompt, started_at) -> None:
    """Persist a stopped turn for a Stop that landed while WAITING on the run
    lock — the api _produce finally skips persistence for the team path
    (``_path["driver"]`` is already set), so without this a Stop-before-start
    leaves the user msg with NO assistant turn on reload."""
    from aiforge_core.runtime import chat_approve, chat_cancel
    chat_approve.clear_emitter(session_id)
    chat_approve.finish(session_id)
    try:
        from aiforge_core.runtime import chat_persist
        chat_persist.persist_turn(
            session_id=session_id, cwd=cwd, prompt=raw_prompt,
            final_text="(stopped before the run started)", steps=[], team=True,
            cancelled=True, awaiting=False, mode="team",
            duration_s=_dur(started_at))
    except Exception:  # noqa: BLE001
        pass
    chat_cancel.finish(session_id)


def _tail_team_queue(q, flags: dict):
    """Yield events off the team run's queue until the sentinel, tracking
    errored/stopped/saw_real in ``flags``. A 10s ``get`` timeout emits a ``ping``
    heartbeat — a slow local model can leave minute-long gaps and without periodic
    output the SSE connection idles and the browser/proxy drops it."""
    while True:
        try:
            item = q.get(timeout=10)
        except queue.Empty:
            yield {"type": "ping"}
            continue
        if item is _SENTINEL:
            return
        if item.get("type") == "error":
            flags["errored"] = True
            if item.get("stopped"):
                flags["stopped"] = True
        else:
            flags["saw_real"] = True
        yield item


def _persist_fallback_turn(session_id, cwd, raw_prompt, fb_final, fb_steps,
                           started_at) -> None:
    """Persist the fallback agent's turn (team _gen skips persistence for team)
    and finish the session's cancel/approve/steer state so nothing leaks into the
    next turn."""
    from aiforge_core.runtime import chat_cancel as _cc
    from aiforge_core.runtime import chat_persist
    cancelled_fb = _cc.is_cancelled(session_id)
    chat_persist.persist_turn(
        session_id=session_id, cwd=cwd, prompt=raw_prompt, final_text=fb_final,
        steps=fb_steps, team=False, cancelled=cancelled_fb, awaiting=False,
        mode="team", duration_s=_dur(started_at))
    _cc.finish(session_id)
    from aiforge_core.runtime import chat_approve as _ca
    from aiforge_core.runtime import chat_interject as _ci
    _ca.finish(session_id)          # a fallback torn down mid-approval would
    _ci.clear(session_id)           # otherwise leak _PENDING/_REVIEW for next turn


def _run_pipeline_fallback(raw_prompt, cwd, session_id, started_at):
    """Run the lightweight single agent as a fallback and yield its events. The
    fallback agent doesn't persist itself, so its answer is persisted here so it
    survives a reload. Best-effort — a fallback failure just ends the stream."""
    try:
        from aiforge_core.runtime import chat_cancel as _cc

        from .chat_agent import run_chat_agent
        if session_id is not None:
            _cc.start(session_id)   # re-arm so Stop can halt the fallback
        yield {"type": "agent", "role": "fallback",
               "text": "(pipeline unavailable — using the lightweight agent)"}
        fb_final = ""
        fb_steps: list[dict] = []
        for ev in run_chat_agent([{"role": "user", "content": raw_prompt}],
                                 cwd=cwd, session_id=session_id):
            if ev.get("type") == "message":
                fb_final = ev.get("text", "")
            elif ev.get("type") in ("thought", "tool", "error"):
                fb_steps.append(ev)
            if ev.get("type") != "done":
                yield ev
        if session_id is not None:
            _persist_fallback_turn(session_id, cwd, raw_prompt, fb_final,
                                   fb_steps, started_at)
    except Exception:
        pass


def _team_plugins() -> list:
    """The ticket driver's plugins: its context filter (keeps a long team run
    inside the model's window — team chat replayed every event on every call)
    plus the perf observer and the phantom-tool guard. Falls back to those
    two alone."""
    try:
        from .adk_runner._pipeline import _build_context_plugins
        plugins = _build_context_plugins()
        if plugins:
            return plugins
    except Exception:  # noqa: BLE001 — resilience is best-effort
        pass
    try:
        from .adk_runner._pipeline import _phantom_tool_guard
        return _phantom_tool_guard()
    except Exception:  # noqa: BLE001
        return []


def _team_deadline_s() -> float:
    """The wall clock for one team turn: ``AIFORGE_CHAT_TEAM_DEADLINE_S``,
    default the ticket pipeline's own deadline (90 min); 0 disables. Team chat
    had none — only an LLM-call cap — so a run stalled below the cap held the
    server-wide team lock indefinitely. (Simple chat's turn deadline defaults
    to OFF, so it is not reused here.)"""
    raw = os.environ.get("AIFORGE_CHAT_TEAM_DEADLINE_S", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        from .adk_runner._verdict import _pipeline_deadline_s
        return float(_pipeline_deadline_s())
    except Exception:  # noqa: BLE001
        return 5400.0
