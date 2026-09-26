"""Calling the model: retries, failure reporting, and the per-step
completion."""
from __future__ import annotations

import os
import time  # noqa: F401  # tests patch _completion.time.sleep

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
    layer (transport retry, empty-answer re-post, this loop's own sweep).

    Retrying a call that is failing for a structural reason does not produce a
    better answer, it produces the same non-answer again at full price — and
    the layers multiply: 5 transport attempts x 4 empty re-posts x 8 sweeps is
    a hundred and sixty generations for one step. This is the ceiling on that
    product. Raised to 10 (from 6) so a genuinely unavailable model — one that
    is reloading or briefly unreachable — gets the fuller retry budget the
    sweeps below now ask for, while still bounding a structurally-failing call;
    0 disables the ceiling and restores the old per-layer behaviour.
    """
    try:
        _v = int(os.environ.get("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "10"))
    except ValueError:
        return 10
    # A NEGATIVE value reads as "tighter than zero" and used to clamp to 0,
    # which means DISABLED — the opposite of what the operator typed. Only an
    # explicit 0 turns the ceiling off; anything else nonsensical falls back to
    # the default rather than silently removing the bound.
    return _v if _v >= 0 else 10


_RETRY_STOP = object()


def _retry_plan(exc, _step_calls):
    """Compute the completion-retry plan: the retry count (env default, capped
    by the per-step generation budget, forced to 0 for a shipped read-timeout or
    an unserved-model config error) plus the per-step budget and a config-error
    message. Returns ``(retries, budget, cfg_error)``."""
    _cfg_error = ""
    _retries = 8
    try:
        _retries = max(0, int(os.environ.get("AIFORGE_CHAT_LLM_RETRIES", "8")))
    except ValueError:
        _retries = 8
    # BOUND THE PRODUCT, not this layer alone. Below this sweep the
    # client already re-posts an empty answer (AIFORGE_LLM_EMPTY_RETRIES,
    # default 3 → 4 posts) and the transport retries a broken one
    # (AIFORGE_LLM_RETRY_MAX, default 3), so five more sweeps here is up
    # to twenty full generations for ONE step — every one of them
    # shipping the whole prompt and generating an answer nobody reads.
    # The meter already counts what this turn has actually sent, so
    # spend the remaining budget instead of a fixed count.
    _spent = int((_step_calls or {}).get("n") or 0)
    _budget = _max_gen_per_step()
    if _budget > 0:      # 0 = ceiling disabled, not "no retries"
        _retries = min(_retries, max(0, _budget - _spent))


    # A read timeout means the model RECEIVED this prompt and is
    # still generating it. Re-issuing the identical completion leaves
    # that generation running and starts another on a box that already
    # could not finish one — five more times, by default. The transport
    # marks the exception; honour it here, or the layer below's
    # "do not re-POST" rule is undone one call up.
    try:
        from aiforge_core.llm.client import shipped_timeout as _st
        if _st(exc):
            _retries = 0
    except Exception:  # noqa: BLE001
        pass
    # A model the endpoint does not serve is CONFIGURATION. Retrying it
    # cannot work — five more full-prompt round trips, each answered
    # with the same 400, then the same useless "didn't respond" line.
    # Say what is wrong instead; the exception already names the model,
    # the endpoint and what that box does serve.
    _cfg_error = ""
    try:
        from aiforge_core.llm.client import model_missing as _mm
        if _mm(exc):
            _retries = 0
            _cfg_error = str(exc).split(" — ", 1)[-1].strip()
    except Exception:  # noqa: BLE001
        pass
    return _retries, _budget, _cfg_error


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
    bound in seconds, <0 = do not wait. ``AIFORGE_CHAT_OUTAGE_WAIT_S`` when set,
    else the shared ``AIFORGE_LLM_WAIT_MAX_S`` (llm/model_wait)."""
    raw = os.environ.get("AIFORGE_CHAT_OUTAGE_WAIT_S", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    from aiforge_core.llm import model_wait
    return model_wait.wait_max_s()


_CANCEL_POLL_S = 0.25


def _outage_waitable(exc) -> bool:
    """The model server is unreachable, reloading, or behind a gateway that
    says it is down — worth waiting for. THE classification lives in
    llm/model_outage; not a call the model is still generating, not a config
    or auth error, not a server that rejects this prompt."""
    try:
        from aiforge_core.llm import model_outage
        return model_outage.classify(exc) == model_outage.OUTAGE
    except Exception:  # noqa: BLE001 — unknown → do not wait
        return False


def _will_wait(wait_s) -> bool:
    """Does this wait setting wait at all? A bound shorter than the first
    probe gap does not (the test suite sets one, so unstubbed calls stay fast)."""
    if wait_s is None or wait_s < 0:
        return False
    from aiforge_core.llm import model_wait
    return wait_s == 0 or wait_s >= next(model_wait.delays())


def _wait_out_outage(complete_fn, role, convo, session_id, exc, wait_s):
    """Wait for the model with the shared backoff (2 s → 5 s → 10 s → 30 s),
    re-trying the completion itself as the probe, until it answers, the error
    stops looking like an outage, the bound (``wait_s`` > 0) runs out, or the
    user presses Stop / types a message. ``wait_s`` 0 = no bound. Yields a
    status line whenever the wait changes (never one per probe); returns
    ``(completion, last_error)``."""
    from aiforge_core.llm import model_wait
    from aiforge_core.runtime import chat_cancel
    from aiforge_core.runtime.run_interrupt import pause
    if session_id is not None and chat_cancel.is_cancelled(session_id):
        return _CANCELLED, None
    import time as _t
    t0 = _t.monotonic()
    shown: dict = {}
    last, waited = exc, 0.0
    for gap in model_wait.delays():
        if wait_s > 0 and waited + gap > wait_s:
            return None, last
        if shown.get("gap") != gap or waited - shown.get("at", 0.0) >= 300:
            shown["gap"], shown["at"] = gap, waited      # a change, or 5 min
            down = model_wait._fmt(_t.monotonic() - t0)
            yield {"type": "thought", "role": "system",
                   "text": f"⏸ waiting for the model (down {down}, next probe "
                           f"{model_wait._fmt(gap)}) — the run continues where "
                           "it left off (Stop ends it)"}
        why = pause(gap, session_id, slice_s=_CANCEL_POLL_S)
        if why is None and model_wait.cancel_reason():
            why = "stop"          # shutdown, a lost ticket claim, a worker stop
        if why == "stop":
            return _CANCELLED, None
        if why == "steer":
            return _STEERED, None
        waited += gap
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
            if out is not _CANCELLED:
                yield {"type": "thought", "role": "system",
                       "text": "▶ the model is back — continuing"}
            return out, None
        except Exception as exc2:  # noqa: BLE001
            last = exc2
        if not _outage_waitable(last):
            return None, last
    return None, last  # pragma: no cover — delays() never ends


def _retry_completion(complete_fn, role, convo, session_id, exc,
                      _step_calls, _meter, _step_tok, wait_s=None, worked=False):
    """Recover a failed model completion: retry (bounded by the per-step
    generation budget; 0 retries for a shipped-timeout or unserved-model error)
    with escalating backoff. A model OUTAGE is waited out instead (``wait_s``:
    None = do not wait, 0 = no bound, >0 = bound in seconds). Yields
    progress/stop events; returns the completion text (possibly None) on
    recovery, or ``_RETRY_STOP`` when the caller must end the turn."""
    from aiforge_core.runtime import chat_cancel
    # RESILIENCE: a local model can transiently drop a request (mid-load,
    # busy, a one-off empty/4xx). Retry a few times before surfacing, and
    # never show the raw `llm.exhausted role=chat …` stack; give a plain,
    # actionable message.
    # AIFORGE_CHAT_LLM_RETRIES tunes the retry count (default 8) — a
    # local model that's loading/busy often needs a few passes.
    _retries, _budget, _cfg_error = _retry_plan(exc, _step_calls)
    def _over_budget() -> bool:
        """Has this STEP spent its generation budget yet?

        Re-read every sweep, because one sweep is not one generation:
        below this loop the client re-posts an empty answer and the
        transport re-attempts a broken one, so a single sweep can burn
        four or twelve. Extrapolating the whole step from the first
        sample let a declared ceiling of 6 spend 12 — the very
        multiplication this exists to stop."""
        if _budget <= 0 or _step_calls is None:
            return False
        return int(_step_calls.get("n") or 0) >= _budget
    out = None
    _last = exc
    # A model OUTAGE is not a bad answer: skip the sweep (its sends would only
    # hit the same dead endpoint and spend the step's budget) and wait.
    if _will_wait(wait_s) and _outage_waitable(exc):
        _retries = 0
    for _rn in range(_retries):
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            return _CANCELLED
        if _over_budget():
            break
        yield {"type": "thought", "role": "system",
               "text": f"⟳ model didn't respond — retrying ({_rn + 1}/{_retries})…"}
        # Escalating backoff, but Stop and a typed message cut it short.
        # The old bare sleep ignored both for the whole backoff (3s, 6s, …).
        from aiforge_core.runtime.run_interrupt import pause
        why = pause(3.0 * (_rn + 1), session_id)
        if why == "stop":
            return _CANCELLED
        if why == "steer":
            return _STEERED
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
            _last = None
            break
        except Exception as exc2:  # noqa: BLE001
            _last = exc2
    if out is _STEERED or out is _CANCELLED:
        return out
    if _last is not None and _will_wait(wait_s) and _outage_waitable(_last):
        out, _last = yield from _wait_out_outage(
            complete_fn, role, convo, session_id, _last, wait_s)
        if out is _STEERED or out is _CANCELLED:
            return out
    if _last is not None:
        yield from _emit_completion_failure(_cfg_error, _meter, _step_tok,
                                            worked=worked and not _cfg_error)
        return _RETRY_STOP
    return out

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
            _step_calls, _meter, _step_tok, wait_s=_wait, worked=_worked)
        if out is _RETRY_STOP:
            return _RETRY_STOP
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
