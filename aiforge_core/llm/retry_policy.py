"""When, how often and for how long a failed model call is tried again.

The layers, innermost first (each has its own counter, and they MULTIPLY):

1. transport   ``AIFORGE_LLM_RETRY_MAX``     re-post a broken request     (llm/client/_http_retry)
2. empty       ``AIFORGE_LLM_EMPTY_RETRIES``  re-post an empty answer      (llm/client/_attempt)
3. sweep       ``AIFORGE_CHAT_LLM_RETRIES``   the chat loop sends it again, bounded by
               ``AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP`` across 1-3 together
4. wait        ``AIFORGE_LLM_WAIT_MAX_S``     an unreachable model is waited for (llm/model_wait)
5. persist     ``AIFORGE_CHAT_PERSIST_S`` / ``AIFORGE_PIPELINE_PERSIST_S``: after the sweep a
               step that keeps failing is tried again, with a smaller prompt, until it
               answers (chat), or the whole candidate chain is walked again (pipeline)

1 and 2 are the leaves and stay where the HTTP code is. This module owns 3-5: the knobs
(:class:`RetryPolicy`, :meth:`RetryPolicy.from_env`), and the one loop that applies them to
a chat model call (:func:`run_with_policy`). What the loop must NOT know is injected
(:class:`Hooks`): how to send again, whether Stop was pressed, how to sleep so a typed message
cuts the sleep short, and how to make the prompt smaller. llm/ therefore never imports runtime/.

The loop is a generator: the status lines it yields reach the user while it is still waiting
(a callback would deliver them after the wait, or hold them back).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

#: Seconds between persist rounds; the last gap repeats until the model answers.
PERSIST_GAPS = (5.0, 10.0, 20.0, 30.0)
#: Rounds a request the server rejects outright (a configuration error) gets.
CONFIG_ROUNDS = 5
#: How often a sleep looks at Stop / a typed message.
CANCEL_POLL_S = 0.25
_BIG = 10 ** 9


# ── the knobs (one reader each; the names and defaults are the old ones) ────

def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _int_or_default(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def transport_attempts() -> int:
    return max(1, _int_or_default("AIFORGE_LLM_RETRY_MAX", 3))


def empty_attempts() -> int:
    """Re-posts of an empty answer (so ``+ 1`` posts in all)."""
    return max(0, _int_or_default("AIFORGE_LLM_EMPTY_RETRIES", 3))


def sweeps() -> int:
    try:
        return max(0, int(os.environ.get("AIFORGE_CHAT_LLM_RETRIES", "8")))
    except ValueError:
        return 8


def generation_budget() -> int:
    """Model generations one step may cost across every retry layer; 0 = no
    ceiling. Only an explicit 0 turns it off; a negative or nonsensical value
    falls back to 10 rather than silently removing the bound."""
    try:
        v = int(os.environ.get("AIFORGE_CHAT_MAX_GENERATIONS_PER_STEP", "10"))
    except ValueError:
        return 10
    return v if v >= 0 else 10


def persist_s(surface: str = "chat") -> float:
    """How long a failing step keeps being tried: 0 = until Stop (default),
    >0 = a bound in seconds, <0 = not at all."""
    return _float("AIFORGE_PIPELINE_PERSIST_S" if surface == "pipeline"
                  else "AIFORGE_CHAT_PERSIST_S", 0.0)


def persist_other_s(surface: str = "chat") -> float:
    """How long a failure that is neither an outage nor a known request error
    is tried again (default 1800)."""
    return max(1.0, _float("AIFORGE_PIPELINE_PERSIST_OTHER_S"
                           if surface == "pipeline"
                           else "AIFORGE_CHAT_PERSIST_OTHER_S", 1800.0))


def attempt_retries() -> int:
    """Tries of one pipeline candidate before the next one (ADK path)."""
    try:
        return max(1, int(os.environ.get("AIFORGE_LLM_ATTEMPT_RETRIES", "1")))
    except ValueError:
        return 1


def demote_after() -> int:
    """Consecutive primary failures before the pipeline skips the primary."""
    try:
        return max(1, int(os.environ.get("AIFORGE_PRIMARY_DEMOTE_AFTER", "2")))
    except (TypeError, ValueError):
        return 2


def attempt_backoff_s(try_no: int) -> float:
    """Sleep after failed try ``try_no`` (0-based) of one pipeline candidate."""
    return min(8.0, 0.5 * (2 ** try_no)) + 0.1


@dataclass(frozen=True)
class RetryPolicy:
    """Every retry/wait number for one surface, read once."""

    transport_attempts: int
    empty_attempts: int
    sweeps: int
    generation_budget: int
    wait_max_s: float
    persist_s: float
    persist_other_s: float
    attempt_retries: int
    demote_after: int

    @classmethod
    def from_env(cls, surface: str = "chat") -> "RetryPolicy":
        """Today's numbers. ``surface`` picks the persist knobs: ``"chat"`` or
        ``"pipeline"``."""
        from aiforge_core.llm import model_wait
        return cls(
            transport_attempts=transport_attempts(),
            empty_attempts=empty_attempts(),
            sweeps=sweeps(),
            generation_budget=generation_budget(),
            wait_max_s=model_wait.wait_max_s(),
            persist_s=persist_s(surface),
            persist_other_s=persist_other_s(surface),
            attempt_retries=attempt_retries(),
            demote_after=demote_after(),
        )


# ── classification (one place; llm/model_outage owns the verdicts) ──────────

def llm_issue(exc):
    try:
        from aiforge_core.llm import model_outage
        return model_outage.issue(exc)
    except Exception:  # noqa: BLE001
        return None


def is_overflow(exc) -> bool:
    try:
        from aiforge_core.llm import model_outage
        return exc is not None and model_outage.is_context_overflow(exc)
    except Exception:  # noqa: BLE001
        return False


def outage_waitable(exc) -> bool:
    """The model server is unreachable, reloading, or behind a gateway that
    says it is down: worth waiting for. Not a failure the client call already
    waited out to its bound: one layer owns the wait."""
    try:
        from aiforge_core.llm import model_outage, model_wait
        return (model_outage.classify(exc) == model_outage.OUTAGE
                and not model_wait.was_waited(exc))
    except Exception:  # noqa: BLE001 — unknown → do not wait
        return False


def was_waited(exc) -> bool:
    try:
        from aiforge_core.llm import model_wait
        return model_wait.was_waited(exc)
    except Exception:  # noqa: BLE001
        return False


def will_wait(wait_s) -> bool:
    """Does this wait setting wait at all? A bound shorter than the first
    probe gap does not."""
    if wait_s is None or wait_s < 0:
        return False
    from aiforge_core.llm import model_wait
    return wait_s == 0 or wait_s >= next(model_wait.delays())


def sweep_plan(policy: RetryPolicy, exc, step_calls) -> "tuple[int, int, str, int]":
    """``(retries, budget, cfg_error, spent0)`` for the sweep after ``exc``.

    The retry count is capped by what the per-step generation budget has left
    (the layers below multiply), forced to 0 for a read timeout (the model has
    the prompt and is still generating it) and for a model the endpoint does not
    serve (configuration: say what is wrong instead)."""
    retries = policy.sweeps
    spent = int((step_calls or {}).get("n") or 0)
    budget = policy.generation_budget
    if budget > 0:      # 0 = ceiling disabled, not "no retries"
        left = max(0, budget - spent)
        if left > 0:
            retries = min(retries, left)
    try:
        from aiforge_core.llm.client import shipped_timeout as _st
        if _st(exc):
            retries = 0
    except Exception:  # noqa: BLE001
        pass
    cfg_error = ""
    try:
        from aiforge_core.llm.client import model_missing as _mm
        if _mm(exc):
            retries = 0
            cfg_error = str(exc).split(" — ", 1)[-1].strip()
    except Exception:  # noqa: BLE001
        pass
    return retries, budget, cfg_error, spent


# ── persist: how many rounds, how long, for which kind of failure ───────────

def persist_gaps():
    for g in PERSIST_GAPS:
        yield g
    while True:
        yield PERSIST_GAPS[-1]


def persist_caps(policy: RetryPolicy, verdict, exc, model_outage) -> "tuple[int, float]":
    """``(max_rounds, max_seconds)`` a failure of this kind earns. An OUTAGE waits
    until the model is back (no cap). The rest can be a request that will never
    work, so each has a limit: a rejected request few rounds, a call the model may
    still be generating (a shipped timeout) fewer, anything else about half an
    hour of tries (``AIFORGE_CHAT_PERSIST_OTHER_S``) — long enough that a flapping
    or overloaded server recovers, short enough that a request that can never
    succeed does not hammer the endpoint for ever."""
    if verdict == model_outage.OUTAGE:
        return _BIG, 0.0
    if verdict == model_outage.CONFIG:
        return CONFIG_ROUNDS, 0.0
    if verdict == model_outage.SHIPPED:
        return 2, 0.0
    if model_outage.issue(exc) is not None:
        return 6, 0.0
    return _BIG, policy.persist_other_s


def pipeline_persist_gap(policy: RetryPolicy, exc, rounds: int,
                         elapsed: float) -> "float | None":
    """Seconds to wait before another sweep of the pipeline's candidate chain,
    or None to give up.

    A stage the model keeps failing must not end the ticket: finishing the task
    comes first. Only a failure that waiting cannot fix (config, auth, a
    rejected request, a cancel) or the bound ends it. ``persist_s``: 0 = until the
    run is cancelled, >0 = a bound in seconds, <0 = off. ``elapsed`` is the time
    since the call started; ``rounds`` the sweeps already waited for."""
    limit = policy.persist_s
    if limit < 0:
        return None
    if elapsed > policy.persist_other_s:
        return None                  # an error that never turns into an answer
    try:
        from aiforge_core.llm import model_wait as _mw
        if _mw.cancel_reason():
            return None
    except Exception:  # noqa: BLE001
        pass
    gap = PERSIST_GAPS[min(rounds, 3)]
    if limit > 0 and elapsed + gap > limit:
        return None
    if exc is not None:
        try:
            from aiforge_core.llm import model_outage as _mo
            if _mo.issue(exc) is not None:
                return None
            kind = _mo.classify(exc)
            if kind in (_mo.CONFIG, _mo.CANCELLED):
                return None
            if kind == _mo.SHIPPED and rounds >= 2:
                return None          # the model may still be generating it
        except Exception:  # noqa: BLE001
            return None
    return gap


# ── the loop ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Hooks:
    """What the loop needs from the caller (a chat turn). Everything that is
    about the conversation, the session or the user lives behind these."""

    #: send the same request again: the completion, or ``stopped`` when Stop cut it
    call: Callable[[], Any]
    #: Stop was pressed (the session's cancel flag)
    cancelled: Callable[[], bool]
    #: sleep up to ``secs``: None when it elapsed, "stop" or "steer" when cut short
    pause: Callable[..., "str | None"]
    #: condense the history in place so the next send is smaller; True if it got smaller
    shrink: Callable[[], bool]
    #: the same prompt failed for size twice: shrink it harder; what was done, or ""
    on_overflow: Callable[[], str]
    #: what ``call`` returns when Stop cut the send
    stopped: Any


class Outcome(NamedTuple):
    kind: str            # one of the constants below
    value: Any = None    # the completion (ANSWER) or the LLM issue (ISSUE)
    cfg_error: str = ""  # FAILED: what is wrong with the configuration, if known


ANSWER, STOPPED, STEERED, ISSUE, FAILED = "answer", "stopped", "steered", "issue", "failed"


def _status(text: str) -> dict:
    return {"type": "thought", "role": "system", "text": text}


def wait_out_outage(hooks: Hooks, exc, wait_s):
    """Wait for the model with the shared backoff (2 s → 5 s → 10 s → 30 s),
    re-trying the completion itself as the probe, until it answers, the error
    stops looking like an outage, the bound (``wait_s`` > 0) runs out, or the
    user presses Stop / types a message. ``wait_s`` 0 = no bound. Yields a
    status line whenever the wait changes (never one per probe); returns
    ``(outcome-or-None, last_error)``: an :class:`Outcome` when the wait ended
    the recovery (an answer, Stop, a typed message), None when it gave up."""
    from aiforge_core.llm import model_wait
    if hooks.cancelled():
        return Outcome(STOPPED), None
    t0 = time.monotonic()
    shown: dict = {}
    last, waited = exc, 0.0
    for gap in model_wait.delays():
        if wait_s > 0 and waited + gap > wait_s:
            return None, last
        if shown.get("gap") != gap or waited - shown.get("at", 0.0) >= 300:
            shown["gap"], shown["at"] = gap, waited      # a change, or 5 min
            down = model_wait._fmt(time.monotonic() - t0)
            yield _status(f"⏸ waiting for the model (down {down}, next probe "
                          f"{model_wait._fmt(gap)}) — the run continues where "
                          "it left off (Stop ends it)")
        why = hooks.pause(gap, CANCEL_POLL_S)
        if why is None and model_wait.cancel_reason():
            why = "stop"          # shutdown, a lost ticket claim, a worker stop
        if why == "stop":
            return Outcome(STOPPED), None
        if why == "steer":
            return Outcome(STEERED), None
        waited += gap
        try:
            out = hooks.call()
            if out is not hooks.stopped:
                yield _status("▶ the model is back — continuing")
            return Outcome(STOPPED if out is hooks.stopped else ANSWER, out), None
        except Exception as exc2:  # noqa: BLE001
            last = exc2
        if not outage_waitable(last):
            return None, last
    return None, last  # pragma: no cover — delays() never ends


def persist_until_answer(hooks: Hooks, policy: RetryPolicy, last, limit_s: float):
    """The model keeps failing this step (the normal retries are spent, or the
    server says this request itself fails). The task still has to finish: wait,
    shrink the prompt, send again — until it answers, the user presses Stop or
    types, or the bound (``limit_s`` > 0) runs out. Yields status lines; returns
    ``(outcome-or-None, last_error)`` where the outcome is None when it gave up."""
    from aiforge_core.llm import model_outage
    t0 = time.monotonic()
    rounds = 0
    for gap in persist_gaps():
        rounds += 1
        if limit_s > 0 and time.monotonic() - t0 + gap > limit_s:
            return None, last
        if hooks.cancelled():
            return Outcome(STOPPED), None
        try:
            verdict = model_outage.classify(last)
        except Exception:  # noqa: BLE001
            verdict = None
        max_rounds, max_s = persist_caps(policy, verdict, last, model_outage)
        if rounds > max_rounds or (max_s and time.monotonic() - t0 > max_s):
            return None, last
        try:
            from aiforge_core.llm import model_wait as _mw
            if _mw.cancel_reason():          # shutdown, a lost ticket claim, a worker stop
                return Outcome(STOPPED), None
        except Exception:  # noqa: BLE001
            pass
        shrunk = hooks.shrink()
        yield _status(f"⏸ the model isn't answering this step — "
                      f"{'condensed the history and ' if shrunk else ''}"
                      f"trying again in {int(gap)}s (attempt {rounds}; Stop ends it)")
        why = hooks.pause(gap, CANCEL_POLL_S)
        if why == "stop":
            return Outcome(STOPPED), None
        if why == "steer":
            return Outcome(STEERED), None
        try:
            out = hooks.call()
            if out is not hooks.stopped:
                yield _status("▶ the model answered — continuing")
            return Outcome(STOPPED if out is hooks.stopped else ANSWER, out), None
        except Exception as exc2:  # noqa: BLE001
            last = exc2
    return None, last  # pragma: no cover — the gap iterator never ends


def run_with_policy(hooks: Hooks, policy: RetryPolicy, exc, *,
                    step_calls=None, wait_s=None):
    """Recover a failed model completion (``exc`` is the first send's failure).

    In order: a request the server keeps failing (an LLM issue) is persisted at,
    not swept; otherwise a bounded sweep of re-sends with escalating backoff
    (bounded by the per-step generation budget; none for a shipped timeout or an
    unserved model), then the persist rounds for an LLM issue, then the outage
    wait (``wait_s``: None = do not wait, 0 = no bound, >0 = bound in seconds),
    then the persist rounds for anything else. Yields status lines; returns an
    :class:`Outcome`."""
    issue = llm_issue(exc)
    if issue is not None:
        if policy.persist_s >= 0:
            res, last = yield from persist_until_answer(
                hooks, policy, exc, policy.persist_s)
            if res is not None:
                return res
            issue = llm_issue(last)
            if issue is None:
                return Outcome(FAILED)
        return Outcome(ISSUE, issue)
    retries, budget, cfg_error, spent0 = sweep_plan(policy, exc, step_calls)
    # Snapshot BEFORE the outage path zeroes `retries`: the planned allowance.
    allowance = retries

    def over_budget() -> bool:
        """Has this STEP spent its generation budget yet?

        Re-read every sweep, because one sweep is not one generation: below this
        loop the client re-posts an empty answer and the transport re-attempts a
        broken one, so a single sweep can burn four or twelve.

        When the failed first call alone already filled the ceiling, allow the
        planned sweeps on top of that sunk spend — otherwise a busy/loading
        model surfaces "didn't respond" with no retry."""
        if budget <= 0 or step_calls is None:
            return False
        spent = int(step_calls.get("n") or 0)
        if spent0 < budget:
            return spent >= budget
        return spent >= spent0 + allowance
    out = None
    last = exc
    # A model OUTAGE is not a bad answer: skip the sweep (its sends would only
    # hit the same dead endpoint and spend the step's budget) and wait. One
    # already waited out to its bound by the client is not re-sent either.
    if will_wait(wait_s) and (outage_waitable(exc) or was_waited(exc)):
        retries = 0
    # The same prompt failing for SIZE twice in a row is not a flaky server:
    # shrink it instead of sending it again.
    over_n = 1 if is_overflow(exc) else 0
    for rn in range(retries):
        if hooks.cancelled():
            return Outcome(STOPPED)
        if over_budget():
            break
        if over_n >= 2:
            over_n = 0
            did = hooks.on_overflow()
            yield _status("⟳ the prompt is over the model's context window "
                          "twice in a row — " + (did or "could not shrink it")
                          + ", sending a smaller one")
        yield _status(f"⟳ model didn't respond — retrying ({rn + 1}/{retries})…")
        # Escalating backoff, but Stop and a typed message cut it short.
        why = hooks.pause(3.0 * (rn + 1))
        if why == "stop":
            return Outcome(STOPPED)
        if why == "steer":
            return Outcome(STEERED)
        try:
            out = hooks.call()
            last = None
            break
        except Exception as exc2:  # noqa: BLE001
            last = exc2
            over_n = over_n + 1 if is_overflow(exc2) else 0
    if last is None and out is hooks.stopped:
        return Outcome(STOPPED)
    issue = llm_issue(last)
    if issue is not None and not cfg_error and policy.persist_s >= 0:
        res, last = yield from persist_until_answer(
            hooks, policy, last, policy.persist_s)
        if res is not None:
            return res
        issue = llm_issue(last)
    if issue is not None:
        return Outcome(ISSUE, issue)
    if last is not None and will_wait(wait_s) and outage_waitable(last):
        res, last = yield from wait_out_outage(hooks, last, wait_s)
        if res is not None:
            return res
        if llm_issue(last) is not None:
            return Outcome(ISSUE, llm_issue(last))
    if last is not None and not cfg_error and policy.persist_s >= 0:
        res, last = yield from persist_until_answer(
            hooks, policy, last, policy.persist_s)
        if res is not None:
            return res
    if last is not None:
        return Outcome(FAILED, cfg_error=cfg_error)
    return Outcome(ANSWER, out)


__all__ = ["RetryPolicy", "Hooks", "Outcome", "run_with_policy", "ANSWER",
           "STOPPED", "STEERED", "ISSUE", "FAILED", "persist_caps",
           "pipeline_persist_gap", "sweep_plan", "wait_out_outage",
           "persist_until_answer", "PERSIST_GAPS", "CONFIG_ROUNDS"]
