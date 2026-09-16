"""Calling the model: retries, failure reporting, and the per-step
completion."""
from __future__ import annotations

import os
import time

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


def _emit_completion_failure(_cfg_error, _meter, _step_tok):
    """Emit the user-facing completion-failure message + the structural
    ``stopped``/``done`` markers (so chat_resume knows the turn died mid-work),
    and release the step meter."""
    yield {"type": "message", "text": (
        f"⚠️ {_cfg_error}" if _cfg_error else
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

def _retry_completion(complete_fn, role, convo, session_id, exc,
                      _step_calls, _meter, _step_tok):
    """Recover a failed model completion: retry (bounded by the per-step
    generation budget; 0 retries for a shipped-timeout or unserved-model error)
    with escalating backoff. Yields progress/stop events; returns the completion
    text (possibly None) on recovery, or ``_RETRY_STOP`` when the caller must end
    the turn."""
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
    for _rn in range(_retries):
        if session_id is not None and chat_cancel.is_cancelled(session_id):
            break
        if _over_budget():
            break
        yield {"type": "thought", "role": "system",
               "text": f"⟳ model didn't respond — retrying ({_rn + 1}/{_retries})…"}
        # Escalating backoff: give a mid-load / busy local model (or a
        # slow compress+forward hop) progressively more room to recover.
        time.sleep(3.0 * (_rn + 1))
        try:
            out = _complete_cancellable(complete_fn, role, convo, session_id)
            _last = None
            break
        except Exception as exc2:  # noqa: BLE001
            _last = exc2
    if _last is not None:
        yield from _emit_completion_failure(_cfg_error, _meter, _step_tok)
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
        out = yield from _retry_completion(
            complete_fn, role, st.convo, session_id, exc,
            _step_calls, _meter, _step_tok)
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
    if out is None:
        out = ""   # a real empty completion — treat as an empty turn
    return out
