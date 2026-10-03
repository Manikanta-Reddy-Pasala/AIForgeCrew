"""Calling the model: retries, failure reporting, and the per-step
completion."""
from __future__ import annotations

import os
import time  # noqa: F401  # tests patch _completion.time.sleep

from aiforge_core.llm import retry_policy

# A retry or outage wait was cut short because the user typed a message.
# The step loop drains it and asks again; this is not a Stop.
from aiforge_core.runtime.run_interrupt import STEERED as _STEERED

from .._context import (
    _CANCELLED,
    _complete_cancellable,
    _complete_live,
)


def _max_gen_per_step() -> int:
    """Total model generations one ReAct step may cost, across every retry
    layer (transport retry, empty-answer re-post, the sweep): the ceiling on
    their product. 0 disables it. See ``llm/retry_policy``."""
    return retry_policy.generation_budget()


_RETRY_STOP = object()


def _retry_plan(exc, _step_calls):
    """``(retries, budget, cfg_error, spent0)`` for the sweep after ``exc``
    (see ``retry_policy.sweep_plan``)."""
    return retry_policy.sweep_plan(retry_policy.RetryPolicy.from_env(), exc,
                                   _step_calls)


def _emit_completion_failure(_cfg_error, _meter, _step_tok, worked=False):
    """Emit the user-facing completion-failure message + the structural
    ``stopped``/``done`` markers (so chat_resume knows the turn died mid-work),
    and release the step meter."""
    yield {"type": "message", "text": (
        f"⚠️ {_cfg_error}" if _cfg_error else
        "⚠️ The model stopped responding. The work done so far is on disk — "
        "send the same message again (or Retry) to continue from there. If it "
        "keeps happening, check the model endpoint." if worked else
        "⚠️ The model didn't respond (it may be loading, busy, or the "
        "request was rejected). Nothing was changed — please try again "
        "in a moment. If it keeps happening, check the model endpoint.")}
    # STRUCTURAL marker, the same one a Stop leaves: this turn ended
    # without an answer, and whatever it had already read or written
    # is on disk. Without it `chat_resume` sees a turn that "ended
    # normally" with a warning as its answer, so Retry starts from
    # nothing and re-does every edit the dead attempt made — the
    # exact case a resume exists for, and the one it was missing.
    yield {"type": "stopped", "reason": "llm_unavailable"}
    yield {"type": "done"}
    if _meter is not None:
        _meter.step_reset(_step_tok)

def _outage_wait_s() -> float:
    """How long a chat run waits for an unreachable model: 0 = until it comes
    back (the default — the run never ends because the model is down), >0 = a
    bound in seconds, <0 = do not wait. ONE knob for every mode:
    ``AIFORGE_LLM_WAIT_MAX_S`` (llm/model_wait). The client call owns the wait;
    this loop waits only for a completion function that does not."""
    from aiforge_core.llm import model_wait
    return model_wait.wait_max_s()


def _emit_llm_issue(issue, _meter, _step_tok):
    """The endpoint is up but keeps failing this request: an LLM ISSUE, not an
    outage. Say so and pause for the user (Retry sends it again) — waiting
    longer would only re-send the same failing request forever."""
    yield {"type": "message", "awaiting_input": True, "text": (
        f"⚠️ LLM issue: {issue}. The model server answers, so this is not an "
        "outage — the request itself keeps failing (a prompt too big for it to "
        "start answering in time, a server that crashes on it, or a gateway "
        "that times it out). The work done so far is on disk. Send the message "
        "again (or Retry) to try once more, or shorten the request / check the "
        "model server.")}
    yield {"type": "stopped", "reason": "llm_request_fails",
           "error": str(issue)}
    yield {"type": "done"}
    if _meter is not None:
        _meter.step_reset(_step_tok)


_llm_issue = retry_policy.llm_issue
_outage_waitable = retry_policy.outage_waitable


def _shrink_for_retry(convo, role, complete_fn, session_id) -> bool:
    """Condense the history in place so the next send is a SMALLER request.
    Returns True when the prompt got shorter. A model that cannot start
    answering a big prompt, or a gateway that times it out, is often fixed by
    this alone; the saved text stays reachable through memory_lookup."""
    try:
        from .._context import _compact_convo
        before = sum(len(str(m.get("content") or "")) for m in convo)
        new = _compact_convo(convo, role=role, complete_fn=complete_fn,
                             session_id=session_id, force=True, keep_recent=6)
        after = sum(len(str(m.get("content") or "")) for m in new)
        if new is not convo and after < before:
            convo[:] = new
            return True
    except Exception:  # noqa: BLE001 — a failed shrink just retries as is
        pass
    return False


def _overflow_restart_enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_CONTEXT_ERROR_RESTART", "1").strip() \
        .lower() not in ("0", "false", "no", "off")


def _shrink_after_overflow(st, complete_fn, role, convo, session_id) -> str:
    """The same prompt just failed for size twice. Do not send it a third
    time: restart from a handoff (``st`` known), else condense. Returns what
    was done, for the status line, or "" when nothing got smaller."""
    if st is not None and _overflow_restart_enabled():
        try:
            from ._escalate import restart_with_handoff
            # The restart REPLACES the context in place; once it has run the
            # run is on the handoff whatever its size, so say so (it used to
            # report "could not shrink it" and then condense the new context).
            if restart_with_handoff(st):
                return "restarted from a handoff"
        except Exception:  # noqa: BLE001 — fall back to the condense
            pass
    if _shrink_for_retry(convo, role, complete_fn, session_id):
        return "condensed the history"
    return ""


def _hooks(complete_fn, role, convo, session_id, st=None):
    """What the retry loop needs from this turn: send again, Stop, an
    interruptible sleep, and the two ways to make the prompt smaller."""
    def pause(secs, slice_s=None):
        from aiforge_core.runtime.run_interrupt import pause as _pause
        if slice_s is None:
            return _pause(secs, session_id)
        return _pause(secs, session_id, slice_s=slice_s)

    def cancelled():
        from aiforge_core.runtime import chat_cancel
        return session_id is not None and chat_cancel.is_cancelled(session_id)

    return retry_policy.Hooks(
        call=lambda: _complete_cancellable(complete_fn, role, convo, session_id),
        cancelled=cancelled, pause=pause,
        shrink=lambda: _shrink_for_retry(convo, role, complete_fn, session_id),
        on_overflow=lambda: _shrink_after_overflow(st, complete_fn, role, convo,
                                                   session_id),
        stopped=_CANCELLED)


def _retry_completion(complete_fn, role, convo, session_id, exc,
                      _step_calls, _meter, _step_tok, wait_s=None, worked=False,
                      st=None):
    """Recover a failed model completion with ``llm/retry_policy`` (the sweep,
    the outage wait, the persist rounds), then say what the user needs to hear
    when it gave up. Yields progress/stop events; returns the completion text
    (possibly None) on recovery, or ``_RETRY_STOP`` when the caller must end the
    turn. ``wait_s``: None = do not wait for an outage, 0 = no bound, >0 = bound
    in seconds."""
    res = yield from retry_policy.run_with_policy(
        _hooks(complete_fn, role, convo, session_id, st),
        retry_policy.RetryPolicy.from_env(), exc,
        step_calls=_step_calls, wait_s=wait_s)
    if res.kind == retry_policy.ANSWER:
        return res.value
    if res.kind == retry_policy.STOPPED:
        return _CANCELLED
    if res.kind == retry_policy.STEERED:
        return _STEERED
    if res.kind == retry_policy.ISSUE:
        yield from _emit_llm_issue(res.value, _meter, _step_tok)
    else:
        yield from _emit_completion_failure(
            res.cfg_error, _meter, _step_tok,
            worked=worked and not res.cfg_error)
    return _RETRY_STOP


def _unboost(reasoning, token) -> None:
    try:
        reasoning._BOOST.reset(token)
    except (ValueError, RuntimeError):      # resumed in another context: nothing to undo
        pass


def _run_completion(st, role, complete_fn, session_id, _meter):
    """Run one model completion: bind the per-step meter, call the model (with the
    bounded retry recovery), reset the meter, and normalise the result. Yields
    retry/stop events; returns the completion text, or _RETRY_STOP to end."""
    # This STEP's own send counter, bound for the duration of the step.
    # Not a delta of the session's turn count: that was inert for every
    # caller without a session (jobs, text_doer, the analysis fan-out —
    # the unattended paths where a storm has nobody watching), refundable
    # by a concurrent turn_reset, and spendable by unrelated same-session
    # traffic.
    _step_calls = None
    _step_tok = None
    if _meter is not None and _max_gen_per_step() > 0:
        try:
            _step_calls = _meter.step_begin()
            _step_tok = _meter.step_bind(_step_calls)
        except Exception:  # noqa: BLE001
            _step_calls, _step_tok = None, None
    from aiforge_core.llm import reasoning as _reasoning
    _boosted = getattr(st, "reason_boost", 0) > 0
    if _boosted:
        st.reason_boost -= 1
    _btok = _reasoning._BOOST.set(_reasoning.boosted() or _boosted)
    try:
        try:
            out = yield from _complete_live(complete_fn, role, st.convo, session_id)
        except Exception as exc:  # noqa: BLE001
            # EVERY run waits out a model outage — fresh or with work done,
            # interactive or background: "if the LLM is not available it should
            # keep on waiting". Stop (or a worker's stop event) ends the wait.
            _worked = st.edits_made > 0
            _w = _outage_wait_s()
            _wait = _w if _w >= 0 else None
            out = yield from _retry_completion(
                complete_fn, role, st.convo, session_id, exc,
                _step_calls, _meter, _step_tok, wait_s=_wait, worked=_worked,
                st=st)
            if out is _RETRY_STOP:
                return _RETRY_STOP
    finally:
        _unboost(_reasoning, _btok)
    # The step's sends are counted; unbind before the next one binds its
    # own (a step that leaves its counter bound would have the NEXT step's
    # calls spend a budget that is already exhausted).
    if _meter is not None:
        _meter.step_reset(_step_tok)
        _step_tok = None

    # H1: Stop pressed DURING generation — the cancellable wrapper returned
    # the sentinel (the abandoned LLM call finishes in the background,
    # ignored). Distinct from a legitimately-empty completion below.
    if out is _CANCELLED:
        yield {"type": "error", "text": "stopped by user"}
        yield {"type": "done"}
        return _RETRY_STOP
    if out is _STEERED:
        return _STEERED
    if out is None:
        out = ""   # a real empty completion — treat as an empty turn
    return out
