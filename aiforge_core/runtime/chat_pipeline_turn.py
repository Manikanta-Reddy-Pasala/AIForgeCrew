"""The team run's turn bookkeeping: its subtask panel, session binding, queue
tail, final answer, the hand-off before the Learner, the persisted turn when it
stops early or falls back, and the event loop the driver thread runs on."""
from __future__ import annotations

import os
import queue
import time
from collections.abc import Callable

from .chat_pipeline_events import _guard_edit_claim, _team_change_events


def _dur(started_at: "float | None") -> "float | None":
    """Per-turn wall-clock seconds since ``started_at`` (None → unknown)."""
    return round(time.time() - started_at, 2) if started_at else None


_SENTINEL = object()
# The driver posted the answer and persisted the turn; only the Learner is still
# running. Ends the client's tail like the sentinel does (see _hand_off_turn).
_HANDED_OFF = object()


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


def _close_turn(session_id, cwd, raw_prompt, final_text, steps, sub_items,
                run_ok, started_at, q, chat_run=None) -> None:
    """Reconcile the subtask panel to the outcome, persist the turn and clear
    the session's approver/cancel/steer state — the latter only while this
    turn's ``chat_run`` is still the session's current run (after a kill-all a
    new turn may own those gates)."""
    from aiforge_core.runtime import chat_cancel
    cancelled = bool(session_id is not None
                     and chat_cancel.is_cancelled(session_id))
    # Reconcile the subtask panel (done on a clean finish, failed on error/stop)
    # — emit live updates AND mutate the persisted item dicts (same objects in
    # `steps`) so a reload shows the same.
    for ev in _finalize_subtasks(sub_items, run_ok, cancelled):
        q.put(ev)
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_persist
            chat_persist.persist_turn(
                session_id=session_id, cwd=cwd, prompt=raw_prompt,
                final_text=final_text, steps=steps, team=True,
                cancelled=cancelled, awaiting=False, mode="team",
                duration_s=_dur(started_at))
        except Exception:  # noqa: BLE001
            pass
        from aiforge_core.runtime import chat_approve, chat_interject, chat_runs
        if chat_run is not None and chat_runs.get(session_id) is not chat_run:
            return
        chat_approve.clear_emitter(session_id)
        chat_approve.finish(session_id)
        chat_cancel.finish(session_id)
        # Team mode does NOT fold steers mid-run — but still clear so a queued
        # steer can't leak into the next turn.
        chat_interject.clear(session_id)


def _hand_off_turn(q, session_id, cwd, raw_prompt, final_text, steps,
                   sub_items, started_at, chat_run=None) -> None:
    """Close the turn while the Learner still runs. The answer is already on
    the queue: reconcile, persist and clear exactly as the teardown would, then
    end the client's tail. The producer finishes the chat run once it has
    published the answer and ``done``, so the UI settles and a follow-up is
    accepted with this answer in its history. The Learner runs on holding the
    team run lock (a team follow-up waits for it) and persists its facts; the
    teardown then only releases the lock.

    The caller marks the run handed off BEFORE calling this, and the marker is
    posted even if closing raises: the turn is persisted at most once, never
    again by the teardown."""
    try:
        _close_turn(session_id, cwd, raw_prompt, final_text, steps, sub_items,
                    True, started_at, q, chat_run)
    finally:
        q.put(_HANDED_OFF)


def _answer_ready(event) -> bool:
    """True when ``event`` means the answer is final: the validator gate routed
    ``done`` (the Learner is next), or the Learner itself spoke. The Learner
    distils facts for memory; nothing it says reaches the answer
    (:func:`_promote_team_answer`)."""
    from .graph_pipeline import ROUTE_DONE
    if getattr(event, "author", None) == "learner":
        return True
    path = getattr(getattr(event, "node_info", None), "path", None) or ""
    node = str(path).rsplit("/", 1)[-1].split("@", 1)[0]
    if node == "learner":
        return True
    route = getattr(getattr(event, "actions", None), "route", None)
    return node == "validator_gate" and route == ROUTE_DONE


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
    """Yield events off the team run's queue until the sentinel (or the
    hand-off), tracking errored/stopped/saw_real/handed_off in ``flags``. A
    10s ``get`` timeout emits a ``ping`` heartbeat — a slow local model can
    leave minute-long gaps and without periodic output the SSE connection idles
    and the browser/proxy drops it."""
    while True:
        try:
            item = q.get(timeout=10)
        except queue.Empty:
            yield {"type": "ping"}
            continue
        if item is _SENTINEL:
            return
        if item is _HANDED_OFF:
            flags["handed_off"] = True
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


async def _close_team_run(agen, runner) -> None:
    """ADK-native stop: aclose() the run generator (cancels the in-flight agent +
    all its sub-agents) and close the runner. Both best-effort."""
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass
    try:
        await runner.close()
    except Exception:  # noqa: BLE001
        pass


def _emit_steer_acks(session_id, chat_interject, q) -> None:
    """Surface a "📌 Got your message" ack for any queued steer (Gap A): the
    Doer/Refiner's before_model callback already folded it into its next model
    call — this just mirrors the ack the simple loop shows, polled once per
    event since the callback has no direct handle to this queue."""
    if session_id is None:
        return
    from aiforge_core.runtime import chat_steer
    for applied in chat_interject.pop_applied(session_id):
        q.put(chat_steer.applied_event(applied))


def _turn_epoch():
    """The request meter's turn epoch bound in THIS context (the producer's), or
    None. The driver runs on a bare thread that inherits no context."""
    try:
        from aiforge_core.llm import call_meter
        return call_meter._TURN_EPOCH.get()
    except Exception:  # noqa: BLE001 — metering never breaks a turn
        return None


def _bind_turn_epoch(epoch) -> None:
    """Stamp the driver thread with its turn's epoch, so its LLM calls — the
    Learner's too, which outlive the turn after a hand-off — bill to this turn
    and never to the session's next one."""
    if epoch is None:
        return
    try:
        from aiforge_core.llm import call_meter
        call_meter.bind_turn((None, epoch))
    except Exception:  # noqa: BLE001
        pass


def _run_async_in_thread(coro_factory: Callable) -> None:
    import asyncio
    loop = asyncio.new_event_loop()

    def _quiet_handler(loop, context):  # noqa: ANN001
        # Swallow litellm LoggingWorker noise (CancelledError / TimeoutError
        # / "task was destroyed") that asyncio would otherwise print to
        # stderr when we tear the loop down. Surface anything else.
        msg = str(context.get("message", "")) + str(context.get("exception", ""))
        if "LoggingWorker" in msg or "logging_worker" in repr(context.get("future", "")):
            return
        exc = context.get("exception")
        if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
            return
        loop.default_exception_handler(context)

    try:
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_quiet_handler)
        loop.run_until_complete(coro_factory())
    finally:
        # Drain leftover background tasks (litellm's LoggingWorker etc.)
        # BEFORE closing — otherwise abruptly closing the loop cancels them
        # mid-flight and spams "Task exception was never retrieved" /
        # "task_done() called too many times".
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.close()
        except Exception:  # noqa: BLE001
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
