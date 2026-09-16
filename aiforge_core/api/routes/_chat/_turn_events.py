"""Consuming a turn's event stream: cleaning, routing, persisting,
and usage reporting."""
from __future__ import annotations

from dataclasses import dataclass

from aiforge_core.runtime.chat_event_slim import slim_event

from ._core import (
    _PRODUCE_SEM,
)
from ._history import (
    _chat_learn_writeback,
    _chat_summarize_session,
)

_TERMINAL_SUBTASK = {"done", "failed", "skipped", "won", "planned"}


def _drive_produce_stream(_events, st: dict, steps: list, run, session_id,
                          turn_t0, turn_mode, clog, emit) -> None:
    """Consume the producer's event stream: clean + route + publish each event,
    honour a mid-stream Stop (coercing in-flight subtasks to a terminal state so
    nothing reloads stuck — "planned" is a settled never-run plan state, left
    alone), then emit the settled usage line, persist the subtask panel, and
    guarantee exactly one terminal ``done``. Own try/except so a stream error
    still surfaces error+done and the caller's finally persists what accumulated.
    """
    from aiforge_core.runtime import chat_cancel
    st["emitted_done"] = False
    try:
        _consume_produce_events(_events, st, steps, run, session_id, turn_t0,
                                turn_mode, clog, emit, chat_cancel)
        _publish_final_usage(run, session_id, steps)
        if st["subtasks"]:
            steps.insert(0, {"type": "subtasks", "items": st["subtasks"]})
        # The UI unblocks on a terminal `done`; a cancelled parallel/best-of-N run
        # breaks before its synthesized one, so guarantee exactly one here when
        # none was forwarded (non-cancel paths emit their own — don't double).
        if not st["emitted_done"]:
            run.publish({"type": "done"})
            st["emitted_done"] = True
    except Exception as exc:  # noqa: BLE001
        run.publish({"type": "error", "text": str(exc)})
        run.publish({"type": "done"})


def _consume_produce_events(_events, st, steps, run, session_id, turn_t0,
                            turn_mode, clog, emit, chat_cancel) -> None:
    """Clean + route + publish each producer event, breaking on a mid-stream Stop
    (coercing in-flight subtasks to a terminal state so nothing reloads stuck).
    "planned" is a settled, never-run plan-mode state — NOT in-flight, left."""
    from aiforge_core.runtime.chat_turn_save import TurnSaver
    saver = TurnSaver(session_id, turn_mode)
    for ev in _events():
        ev = _clean_and_log_produce_event(ev, session_id, turn_t0, turn_mode,
                                          clog, emit)
        if ev is None:
            continue
        _route_produce_event(ev, st, steps)
        run.publish(ev)
        saver.maybe_save(steps, st["subtasks"])
        if chat_cancel.is_cancelled(session_id):
            for row in st["subtasks"]:
                if row.get("status") not in _TERMINAL_SUBTASK:
                    row["status"] = "failed"
            break


def _mirror_event_to_log(ev, session_id, clog, emit):
    """Mirror a substantive producer event (thought/tool/message/error) into the observability NDJSON. Best-effort."""
    if clog is not None and emit is not None and \
            ev.get("type") in ("thought", "tool", "message", "error"):
        try:
            emit(clog, ev["type"], session=session_id, name=ev.get("name"),
                 text=(ev.get("text") or "")[:200],
                 tool_ok=(ev.get("result") or {}).get("ok")
                 if isinstance(ev.get("result"), dict) else None)
        except Exception:  # noqa: BLE001
            pass


def _clean_and_log_produce_event(ev, session_id, turn_t0, turn_mode, clog, emit):
    """Sanitise + timestamp + log-mirror one producer event. Returns the event
    (possibly rewritten) or None to DROP it.

    Never surface leaked protocol scaffolding: a local model in native-FC mode
    sometimes emits a fumbled tool call as plain content ("ARGS_JSON: {}", a bare
    "ACTION:"). Strip marker-only noise from a non-ASK message; if nothing real
    remains, drop it so it neither streams nor persists as the answer. Stamp the
    terminal event's server-authoritative wall-clock and mirror substantive
    events into the observability NDJSON."""
    import time as _time
    if ev.get("type") == "message" and not ev.get("awaiting_input"):
        from aiforge_core.runtime.chat_agent._prompt import _strip_protocol_noise
        clean = _strip_protocol_noise(ev.get("text") or "")
        if not clean:
            return None
        if clean != ev.get("text"):
            ev = {**ev, "text": clean}
    if ev.get("type") == "done" and "elapsed_s" not in ev:
        ev = {**ev, "elapsed_s": round(_time.time() - turn_t0, 2),
              "mode": turn_mode}
    _mirror_event_to_log(ev, session_id, clog, emit)
    return ev


def _route_produce_event(ev, st: dict, steps: list) -> None:
    """Route one cleaned producer event into the turn accumulators. A plain
    message becomes the persisted ``final_text``; a supplementary message /
    thought / tool / error / changes / stopped marker / plan_ready / captured pill
    is persisted as a step; subtasks/subtask_update maintain the live panel.
    ("stopped" is a MARKER not a rendered step — it tells Retry there is work on
    disk to resume; dropped, the resume silently never happens.)"""
    etype = ev.get("type")
    if etype == "message" and not ev.get("supplementary"):
        st["final_text"] = ev.get("text", "")
        st["awaiting"] = bool(ev.get("awaiting_input"))
    elif (etype == "message" and ev.get("supplementary")
          or etype in ("thought", "tool", "error", "changes", "stopped",
                       "plan_ready", "captured")):
        steps.append(slim_event(ev))
    elif etype == "subtasks":
        st["subtasks"] = list(ev.get("items") or [])
    elif etype == "subtask_update":
        for row in st["subtasks"]:
            if row.get("slug") == ev.get("slug"):
                row["status"] = ev.get("status")
    if etype == "done":
        st["emitted_done"] = True


@dataclass(frozen=True)
class _TurnResetContext:
    """The per-turn meter boundary + session/repo-root contextvar tokens, bound
    at turn start and reset together at turn end. Bundled so the producer's
    finally forwards ONE handle instead of five loose tokens that always travel
    as a unit."""
    meter: object
    meter_token: object
    reqctx: object
    sess_token: object
    repo_token: object


def _reset_turn_context(ctx: _TurnResetContext) -> None:
    """Reset the per-turn meter boundary + the session/repo-root contextvars.
    Each soft-fails independently."""
    try:
        if ctx.meter is not None:
            ctx.meter.reset_turn(ctx.meter_token)
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx.reqctx.reset_session_id(ctx.sess_token)
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx.reqctx.reset_repo_root(ctx.repo_token)
    except Exception:  # noqa: BLE001
        pass


def _persist_produce_turn(session_id, cwd, prompt, final_text, steps, awaiting,
                          team, path, turn_mode, turn_t0, cancelled) -> None:
    """Finish the session's cancel/approve/steer gates, persist the turn, and
    kick the simple/plan memory writebacks (skipping cancelled turns and the
    parallel-team path, whose own runners cover it)."""
    import time as _time

    from aiforge_core.runtime import chat_approve, chat_cancel, chat_interject, chat_persist
    chat_cancel.finish(session_id)
    chat_interject.clear(session_id)   # no stale steers next turn
    chat_approve.finish(session_id)
    chat_persist.persist_turn(
        session_id=session_id, cwd=cwd, prompt=prompt, final_text=final_text,
        steps=steps, team=(team or path["parallel"]), cancelled=cancelled,
        awaiting=awaiting, mode=turn_mode, duration_s=_time.time() - turn_t0)
    if not cancelled and not team and not path["parallel"]:
        from functools import partial as _partial

        from aiforge_core.runtime import background as _bg
        # Single-chat (simple/plan) memory writeback on daemon threads — the team
        # pipeline runs a Learner node itself; the inline path never did, so chat
        # work never reached long-term memory. The boundary-gated per-session
        # summary refreshes cross-session recall's graph copy every N turns.
        _bg.spawn(_partial(_chat_learn_writeback, cwd, prompt, final_text, steps,
                           session_id), name="chat-learn")
        _bg.spawn(_partial(_chat_summarize_session, cwd, session_id),
                  name="chat-summarize")


def _finalize_produce_turn(session_id, cwd, prompt, final_text, steps, awaiting,
                           team, path, _turn_mode, _turn_t0, _reset_ctx, run,
                           _awake_release) -> None:
    """The producer's finally: emit the turn-outcome Langfuse score, reset the
    meter/session/repo-root state, and (for every mode EXCEPT the team driver,
    which self-persists on its own thread) finish the cancel/approve/steer gates,
    persist the turn and kick the simple/plan memory writebacks. Then wake
    subscribers and release the keep-awake + producer-slot holds. Every side
    channel soft-fails — the finally must never break a turn."""
    from aiforge_core.runtime import chat_cancel
    # Capture cancellation BEFORE finishing the token (finish pops
    # it, after which is_cancelled always reads False).
    cancelled = chat_cancel.is_cancelled(session_id)
    # Emit one turn-outcome score per run so the Langfuse Scores view
    # populates (0.0 stopped, 1.0 completed), tagged to this session.
    # Side-channel: soft-fails, never affects the turn. Runs for every
    # mode (this finally is hit inline for simple/plan/parallel and for
    # a team run whether or not the ADK driver launched).
    try:
        from aiforge_core.integrations import langfuse_adapter as _lf
        if _lf.enabled():
            _lf.record_score(
                name="turn_completed",
                value=0.0 if cancelled else 1.0,
                session_id=session_id,
                comment="cancelled" if cancelled else "completed",
                metadata={"mode": _turn_mode})
    except Exception:  # noqa: BLE001 — tracing must never break a turn
        pass
    _reset_turn_context(_reset_ctx)
    # TEAM mode: the background driver owns the run's lifetime AND its
    # persistence (chat_pipeline._drive) — it survives a client
    # disconnect and holds the real final answer, so we must NOT
    # persist a partial here (and finishing the token here would
    # orphan a still-running ADK run on Stop). SIMPLE mode runs inline
    # in this producer thread, so finish + persist here.
    # Parallel team mode is a self-contained generator (not the
    # background ADK driver), so persist it inline like simple mode.
    # The sequential fallback uses the team driver, which self-persists.
    # Gate on whether that driver actually LAUNCHED — a team run that
    # crashes in the pre-stream orchestrator (enhance/architect/
    # decompose) never starts the driver, so it must clean up here too.
    # TEAM mode: the background driver owns the run lifetime AND persistence
    # (chat_pipeline._drive) — it survives a client disconnect and holds the real
    # answer, so we must NOT persist a partial here (finishing the token would
    # also orphan a still-running ADK run on Stop). SIMPLE/plan run inline here;
    # parallel-team is a self-contained generator, so both persist inline. Gate
    # on whether the driver actually LAUNCHED — a team run that crashes in the
    # pre-stream orchestrator never starts it and must clean up here too.
    if not path["driver"]:
        _persist_produce_turn(session_id, cwd, prompt, final_text, steps,
                              awaiting, team, path, _turn_mode, _turn_t0,
                              cancelled)
    # The turn ended here (or its driver owns persistence): the crash copy
    # must not come back as a second, "interrupted" answer.
    try:
        from aiforge_core.runtime.chat_turn_save import TurnSaver
        TurnSaver(session_id).discard()
    except Exception:  # noqa: BLE001
        pass
    # Wake every subscriber (this stream + any /attach) and close THIS
    # run object (not by session id — a newer turn for the same session
    # may have already replaced it in the registry). Done LAST so a
    # re-attach during persistence still tails live.
    #
    # …except when the team driver owns the run: it is still working on a
    # background thread here, and finishing its run made the box look IDLE for
    # the rest of the turn — the idle compactor then folded memory mid-run
    # (LLM calls and rate-limit budget) while the team was still calling tools.
    # chat_pipeline._drive_teardown finishes it, in the driver's own finally,
    # so a crash still wakes every subscriber.
    if not path["driver"]:
        run.finish()
    try:
        _awake_release()
    except Exception:  # noqa: BLE001 — power policy never fails a turn
        pass
    try:
        _PRODUCE_SEM.release()
    except (ValueError, RuntimeError):   # never over-release
        pass


def _usage_step_text(calls: dict) -> str:
    """The persisted ``⚡ N LLM requests`` line. Failed attempts are NAMED in the
    same line (12 requests of which 7 failed is a retry storm; a bare "12" reads
    thorough), and tokens-written sits next to the count (40 one-line steps and
    one 6000-token essay are both "41 requests")."""
    failed = int(calls.get("turn_failed") or 0)
    out_tok = int(calls.get("turn_tokens_out") or 0)
    if out_tok >= 1000:
        tok_txt = f", {out_tok / 1000:.1f}k tokens written"
    elif out_tok:
        tok_txt = f", {out_tok} tokens written"
    else:
        tok_txt = ""
    noun = "request" if calls["turn"] == 1 else "requests"
    return (f"⚡ {calls['turn']} LLM {noun} for this message "
            f"({calls['session']} in this chat"
            + (f", {failed} failed" if failed else "") + tok_txt + ")")


def _publish_final_usage(run, session_id, steps: list) -> None:
    """Publish + persist the SETTLED per-turn LLM request/token count. The in-loop
    ``usage`` events fire BEFORE each model call, so the last one under-reports by
    the answer's own call (plus retries); this emits the true numbers once the run
    is over. Also persisted as a step — the live badge dies with liveTurn a few
    hundred ms later and usage events aren't persisted, so without this the number
    the user is left looking at is gone from any reload. Failed attempts are named
    in the same line (a retry storm reads like a thorough turn otherwise) and
    tokens-written sits next to the count (40 one-line steps and one 6000-token
    essay are both "41 requests"). Metering must never break a turn."""
    # FINAL request count. The in-loop `usage` events fire BEFORE each
    # model call, so the last one always under-reports by at least the
    # answer's own call (plus any retry it needed). Emit the settled
    # numbers once the run is over, so the count the user is left
    # looking at is the true one.
    try:
        from aiforge_core.llm import call_meter as _cm
        _calls = _cm.snapshot(session_id)
        run.publish({"type": "usage", "llm_turn": _calls["turn"],
                     "llm_session": _calls["session"],
                     "llm_per_min": _calls["per_minute"],
                     "llm_turn_failed": _calls.get("turn_failed", 0),
                     "llm_failed_per_min":
                         _calls.get("failed_per_minute", 0),
                     "llm_turn_tokens_out":
                         _calls.get("turn_tokens_out", 0),
                     "final": True})
        # …and PERSIST it as a step. The live badge dies with liveTurn
        # a few hundred ms later (loadSession + setLiveTurn(null)), and
        # usage events are not persisted — so without this the settled
        # number the user is meant to be left looking at is gone from
        # the transcript and from any later reload.
        if _calls["turn"]:
            steps.append({"type": "thought", "role": "system",
                          "text": _usage_step_text(_calls)})
    except Exception:  # noqa: BLE001 — metering must never break a turn
        pass
