"""Wall-clock + LM-call + LOC kill switches for the Doer LoopAgent.

The LoopAgent caps iterations at 3 by default, but ONE-117 and ONE-1
(audit subsystem) both showed that even a small iteration cap doesn't
bound wall-clock or LM-call count when a SINGLE Doer turn keeps
calling tools without ever returning a final response. ONE-1 burned
500 LM calls in one mega-iteration over ~4 hours (file-write phase
done in 1.5h, then ~2h stuck in `mvn compile` / `mvn test` red-loop)
before ADK's hard ``Runner.max_llm_calls=500`` cap finally killed it.

The first attempt at a per-LM-call budget (PR #25) stored the counter
in ``callback_context.state['llm_call_count']``. **That broke under
ONE-1's PR #25 re-test:** despite 440+ ADK invocations the counter
never accumulated past a few units and the 400 trip never fired.
Root cause is contested — most likely candidate is that the LoopAgent
flushes session-state delta on iteration boundaries in a way that
loses single-turn deltas, but proving it requires an ADK contributor
spelunk we can't justify. The pragmatic fix is to **NOT rely on ADK
session state** for the counter at all: a module-level dict keyed by
``InvocationContext.invocation_id`` survives the entire Runner
lifetime regardless of LoopAgent / Refiner / Doer state plumbing.

This module supplies THREE complementary watchdogs, each hooked at a
different ADK checkpoint so a stuck loop trips at least one of them:

1. **LOC-plateau** (``after_iteration_callback`` on the Refiner):
   reads Doer ``file_diffs``, tracks ``state['loc_history']``. Trips
   when 3 consecutive iterations show |delta| < 50 LOC AND elapsed
   > 600s. Targets the "edit-revert-edit" failure mode where each
   iteration genuinely returns but makes no real progress. Catches
   inter-iteration plateaus. (Still uses session state — works at
   iteration boundary where state.delta does flush.)

2. **LM-call + wall-clock** (``before_model_callback`` on the Doer):
   increments a MODULE-LEVEL counter on EVERY LLM call. Trips when
   count >= ``AIFORGE_LOOP_LLM_CALL_BUDGET`` (default 400) OR
   elapsed > ``AIFORGE_LOOP_WALL_BUDGET_S`` (default 5400 = 90 min).
   Targets the "single mega-iteration" failure mode where the Doer
   never returns and so the iteration-boundary watcher above never
   fires. Catches intra-iteration runaway.

3. **Kill-flag short-circuit** (``before_agent_callback`` on the
   LoopAgent): reads the kill flag set by either watcher above and
   short-circuits the next iteration with ``verdict=partial`` so the
   adk_runner's commit-and-PR path takes whatever's on disk to GitHub
   for human triage instead of dropping the work on the floor.

Env knobs (all optional):
  - ``AIFORGE_LOOP_LOC_PLATEAU_TURNS``   default 3      (LOC plateau)
  - ``AIFORGE_LOOP_LOC_PLATEAU_DELTA``   default 50     (LOC plateau)
  - ``AIFORGE_LOOP_MIN_ELAPSED_S``       default 600    (LOC plateau)
  - ``AIFORGE_LOOP_LLM_CALL_BUDGET``     default 400    (LM-call cap)
  - ``AIFORGE_LOOP_WALL_BUDGET_S``       default 5400   (wall-clock 90m)
  - ``AIFORGE_LOOP_BUDGET_DISABLE=1``    disables ALL watchdogs

State shape:

    Session state (LoopAgent-aware, used by LOC plateau watcher):
        state['loc_history']        -> list[int]    # LOC per iteration
        state['loc_first_seen']     -> float        # epoch sec of turn 0
        state['loop_budget_kill']   -> bool         # set when ANY watcher fires
        state['loop_budget_reason'] -> str          # short tag for traces

    Module-level (PROCESS-aware, used by LM-call + wall-clock watcher):
        _CALL_COUNTERS[bucket_key] -> {'count': int, 'first_at': float}

The kill flag is monotonic — once set, never cleared. Idempotent so a
stuck callback that fires twice doesn't double-emit the partial verdict.

The module-level counter dict is not auto-reset between tickets — but
the runner is single-shot per ticket (systemd Restart=always), so each
new ticket gets a fresh process and a fresh dict. Tests can clear it
explicitly via :func:`reset_call_counters`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable


log = logging.getLogger("aiforge.loop_budget")


# Defaults — env knobs override at callback-build time so tests can
# tweak them without monkey-patching env between asserts.
_DEFAULT_PLATEAU_TURNS = 3
_DEFAULT_PLATEAU_DELTA = 50
_DEFAULT_MIN_ELAPSED_S = 600.0

# LM-call budget — 400 trips before ADK's own hard 500 cap so we get
# a clean partial-verdict commit rather than an exception traceback.
# Wall-clock — 90min default chosen to comfortably bound a 5K-LOC
# scaffold (ONE-1 hit ~4h before its mega-turn finally died).
_DEFAULT_LLM_CALL_BUDGET = 400
_DEFAULT_WALL_BUDGET_S = 5400.0


# Module-level counter dict, keyed by an opaque "bucket key" (typically
# the ADK ``invocation_id``). One entry per Runner.run_async invocation.
# We do NOT use ``callback_context.state`` for the counter because the
# LoopAgent/Refiner/Doer state plumbing was empirically losing the
# delta on PR #25's ONE-1 re-test (440 LM calls observed, counter never
# accumulated). Module-level dict survives ALL ADK plumbing.
#
# Single-shot runner means each ticket is a fresh process and a fresh
# dict — no cross-ticket pollution. Tests reset via reset_call_counters().
_CALL_COUNTERS: dict[str, dict[str, float]] = {}


def reset_call_counters() -> None:
    """Test-only — drop all module-level call counters.

    The runner is single-shot per ticket so production code never needs
    this; tests do, otherwise a budget-trip in test N is still set when
    test N+1 runs.
    """
    _CALL_COUNTERS.clear()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("loop_budget: bad int env %s=%r — using default %d",
                    name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("loop_budget: bad float env %s=%r — using default %s",
                    name, raw, default)
        return default


def _coerce_doer_outcome(value: Any) -> dict | None:
    """Doer ``output_key`` may land as a dict (ADK auto-parsed JSON)
    or as a raw string the model emitted. Try both — anything else
    means "no diff this turn"."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return None
            if isinstance(obj, dict):
                return obj
    return None


def _loc_for_turn(state: dict) -> int:
    """Compute a cumulative LOC count for the most recent Doer turn.

    Strategy: sum line counts of every file_diffs entry's content.
    The Doer's prompt contract emits ``file_diffs: [{path, action,
    content?}, ...]`` — when ``content`` isn't there (e.g. action=
    ``patch`` reporting a delta) we fall back to counting unique
    paths so a no-content patch turn still differs from a no-op turn.
    """
    outcome = _coerce_doer_outcome(state.get("doer_outcome"))
    if not outcome:
        return 0
    diffs = outcome.get("file_diffs") or []
    if not isinstance(diffs, list):
        return 0
    total = 0
    for entry in diffs:
        if not isinstance(entry, dict):
            continue
        # Prefer explicit LOC if the Doer reports it (forward compat).
        loc = entry.get("loc")
        if isinstance(loc, int):
            total += loc
            continue
        content = entry.get("content")
        if isinstance(content, str):
            # newline count is a stable, model-agnostic LOC proxy.
            total += content.count("\n") + 1
            continue
        # patch w/o content reported — count as 1 so we DO see motion.
        total += 1
    return total


def _record_history(state: dict, loc: int, *, now: float | None = None) -> None:
    """Append ``loc`` to ``state['loc_history']`` and stamp the first
    observation time. Keeps the history capped so a runaway loop
    can't balloon session state."""
    history = state.setdefault("loc_history", [])
    if not isinstance(history, list):
        # Caller put something else in the slot — overwrite to keep
        # the contract simple; warn loudly so the bug surfaces.
        log.warning("loop_budget: state['loc_history'] was %r — resetting",
                    type(history).__name__)
        state["loc_history"] = history = []
    history.append(int(loc))
    # Cap at 32 entries — way more than the plateau window needs.
    if len(history) > 32:
        del history[: len(history) - 32]
    state.setdefault("loc_first_seen", now if now is not None else time.time())


def _plateau_hit(history: list[int], turns: int, delta: int) -> bool:
    """``True`` when the last ``turns`` consecutive deltas in
    ``history`` are all strictly less than ``delta``. Needs at least
    ``turns + 1`` entries to evaluate."""
    if turns <= 0 or delta < 0:
        return False
    if len(history) < turns + 1:
        return False
    tail = history[-(turns + 1):]
    for prev, cur in zip(tail, tail[1:]):
        if abs(cur - prev) >= delta:
            return False
    return True


def evaluate_plateau(
    state: dict,
    *,
    plateau_turns: int = _DEFAULT_PLATEAU_TURNS,
    plateau_delta: int = _DEFAULT_PLATEAU_DELTA,
    min_elapsed_s: float = _DEFAULT_MIN_ELAPSED_S,
    now: float | None = None,
) -> bool:
    """Pure helper — record the current turn's LOC and return whether
    the kill switch should fire. Mutates ``state`` in place. Lives
    outside the callback so unit tests can drive it without ADK.

    The state mutations:
      - appends current LOC to ``state['loc_history']``
      - sets ``state['loop_budget_kill']`` and
        ``state['loop_budget_reason']`` when plateau hits.

    Returns ``True`` when this call SET the kill flag (transition);
    returns ``False`` if the flag was already set or wasn't set this
    turn. Callers don't strictly need the return value — they read
    the flag — but it's handy for tests.
    """
    if state.get("loop_budget_kill"):
        return False  # already killed; idempotent

    loc = _loc_for_turn(state)
    _record_history(state, loc, now=now)
    history = state["loc_history"]

    elapsed = (now if now is not None else time.time()) - state["loc_first_seen"]
    if elapsed < min_elapsed_s:
        return False
    if not _plateau_hit(history, plateau_turns, plateau_delta):
        return False

    state["loop_budget_kill"] = True
    state["loop_budget_reason"] = (
        f"loc_plateau:{plateau_turns}x<{plateau_delta}_after_{int(elapsed)}s"
    )
    log.warning("loop_budget: plateau hit history_tail=%s elapsed=%.1fs — "
                "force commit + verdict=partial",
                history[-(plateau_turns + 1):], elapsed)
    return True


def evaluate_call_budget(
    state: dict,
    *,
    bucket_key: str = "default",
    llm_call_budget: int = _DEFAULT_LLM_CALL_BUDGET,
    wall_budget_s: float = _DEFAULT_WALL_BUDGET_S,
    now: float | None = None,
) -> bool:
    """Pure helper — increment the module-level call counter and return
    whether the LM-call OR wall-clock budget kill switch should fire.

    Counter lives in :data:`_CALL_COUNTERS` keyed by ``bucket_key`` (the
    ADK ``invocation_id`` in production). The kill flag is mirrored to
    ``state['loop_budget_kill']`` / ``state['loop_budget_reason']`` so
    the LoopAgent's ``before_agent_callback`` can read it and short-
    circuit at the next iteration boundary.

    Mutations on success:
      - bumps ``_CALL_COUNTERS[bucket_key]['count']``
      - stamps ``_CALL_COUNTERS[bucket_key]['first_at']`` on first call
      - sets ``state['loop_budget_kill']`` and
        ``state['loop_budget_reason']`` when either budget trips
      - mirrors ``state['llm_call_count']`` for trace visibility (best
        effort — the source of truth is the module-level counter so
        ADK's state-delta drop won't break the kill check)

    Returns ``True`` when this call SET the kill flag (transition);
    ``False`` if the flag was already set or no budget tripped.

    Idempotent — safe to call after the kill flag is already raised.
    Lives outside the callback so unit tests can drive it without ADK.
    """
    bucket = _CALL_COUNTERS.setdefault(bucket_key, {})

    if "first_at" not in bucket:
        bucket["first_at"] = float(now if now is not None else time.time())

    bucket["count"] = float(int(bucket.get("count", 0)) + 1)
    count = int(bucket["count"])

    # Best-effort mirror to session state for trace visibility — even if
    # the LoopAgent eats the delta, an iteration-boundary trace still
    # shows a recent count value (the most recent flushed delta).
    try:
        state["llm_call_count"] = count
    except Exception:  # noqa: BLE001 — state can be a wrapper that rejects writes
        pass

    if state.get("loop_budget_kill"):
        return False

    elapsed = (
        (now if now is not None else time.time())
        - bucket["first_at"]
    )

    if llm_call_budget > 0 and count >= llm_call_budget:
        state["loop_budget_kill"] = True
        state["loop_budget_reason"] = (
            f"llm_call_budget:{count}/{llm_call_budget}_after_{int(elapsed)}s"
        )
        log.warning(
            "loop_budget: LM-call budget hit count=%d/%d elapsed=%.1fs "
            "bucket=%s — force commit + verdict=partial (before ADK 500 "
            "hard cap)",
            count, llm_call_budget, elapsed, bucket_key,
        )
        return True

    if wall_budget_s > 0 and elapsed >= wall_budget_s:
        state["loop_budget_kill"] = True
        state["loop_budget_reason"] = (
            f"wall_budget:{int(elapsed)}s>={int(wall_budget_s)}s_count={count}"
        )
        log.warning(
            "loop_budget: wall-clock budget hit elapsed=%.1fs/%.1fs "
            "count=%d bucket=%s — force commit + verdict=partial",
            elapsed, wall_budget_s, count, bucket_key,
        )
        return True

    return False


def build_loop_budget_callbacks() -> tuple[
    Callable[..., Any] | None,
    Callable[..., Any] | None,
    Callable[..., Any] | None,
]:
    """Wire the three watchdogs into the v6 pipeline.

    Returns
        ``(before_loop_callback, after_iteration_callback,
        before_doer_model_callback)``.

    All three are ``None`` when the watchdog is env-disabled. Callers
    branch on the first element only — if it's ``None`` the others
    are too, matching the original 2-tuple contract callers used
    before the per-LM-call watcher was added.

    Wiring map (caller's responsibility — see :mod:`pipeline`):
      - ``before_loop_callback``     → LoopAgent.before_agent_callback
      - ``after_iteration_callback`` → Refiner.after_agent_callback
      - ``before_doer_model_callback`` → Doer.before_model_callback

    The before-loop callback short-circuits the LoopAgent when the
    kill flag is set (next iteration never starts). The after-
    iteration callback observes inter-iteration LOC plateau. The
    before-model callback observes per-LM-call wall-clock + count
    so a single runaway iteration trips even without a Refiner turn.
    """
    if os.environ.get("AIFORGE_LOOP_BUDGET_DISABLE", "0") in ("1", "true"):
        return None, None, None

    plateau_turns = _env_int("AIFORGE_LOOP_LOC_PLATEAU_TURNS",
                             _DEFAULT_PLATEAU_TURNS)
    plateau_delta = _env_int("AIFORGE_LOOP_LOC_PLATEAU_DELTA",
                             _DEFAULT_PLATEAU_DELTA)
    min_elapsed_s = _env_float("AIFORGE_LOOP_MIN_ELAPSED_S",
                               _DEFAULT_MIN_ELAPSED_S)
    llm_call_budget = _env_int("AIFORGE_LOOP_LLM_CALL_BUDGET",
                               _DEFAULT_LLM_CALL_BUDGET)
    wall_budget_s = _env_float("AIFORGE_LOOP_WALL_BUDGET_S",
                               _DEFAULT_WALL_BUDGET_S)

    async def after_iteration_callback(*, callback_context):  # type: ignore[no-untyped-def]
        """Refiner's after-agent hook — observe LOC, flip kill flag."""
        state = callback_context.state
        try:
            evaluate_plateau(
                state,
                plateau_turns=plateau_turns,
                plateau_delta=plateau_delta,
                min_elapsed_s=min_elapsed_s,
            )
        except Exception as exc:  # never let watchdog crash the loop
            log.exception("loop_budget.after_iteration_callback failed: %s", exc)
        return None  # don't override the refiner's content

    async def before_doer_model_callback(*, callback_context, llm_request):  # type: ignore[no-untyped-def]
        """Doer's before-model hook — count LM calls + check budgets.

        Fires on EVERY LLM call the Doer makes (one call per tool
        roundtrip + one per reasoning step). When the LM-call budget
        or wall-clock budget trips, sets the kill flag — the loop's
        ``before_agent_callback`` will short-circuit the NEXT
        iteration. We don't short-circuit the current LM call here
        because we want the Doer to finish its current tool turn
        cleanly so the partial work is on disk before the rescue
        path commits.

        Counter is keyed on ``InvocationContext.invocation_id`` so
        the value persists across LoopAgent iteration boundaries
        (which the previous session-state implementation didn't —
        see PR #25 ONE-1 re-test postmortem in module docstring).

        ``llm_request`` is the raw model request — we don't mutate
        it. ADK's ``BeforeModelCallback`` signature requires the
        param even when unused.
        """
        del llm_request  # unused — required by callback signature
        state = callback_context.state
        # Extract invocation_id robustly. ADK 2.0b1 exposes it via
        # ``invocation_context`` on the CallbackContext; older builds
        # may not have the same attribute path. Default to the
        # ticket's ADK app name + a fixed suffix so the bucket is
        # still ticket-scoped if introspection fails.
        bucket_key = "default"
        for attr_chain in (
            ("invocation_context", "invocation_id"),
            ("_invocation_context", "invocation_id"),
        ):
            obj: Any = callback_context
            for attr in attr_chain:
                obj = getattr(obj, attr, None)
                if obj is None:
                    break
            if isinstance(obj, str) and obj:
                bucket_key = obj
                break
        try:
            evaluate_call_budget(
                state,
                bucket_key=bucket_key,
                llm_call_budget=llm_call_budget,
                wall_budget_s=wall_budget_s,
            )
        except Exception as exc:  # never let watchdog crash the loop
            log.exception(
                "loop_budget.before_doer_model_callback failed: %s", exc,
            )
        return None  # never short-circuit the call itself

    async def before_loop_callback(*, callback_context):  # type: ignore[no-untyped-def]
        """LoopAgent's before-agent hook — short-circuit on kill flag.

        Returning a ``types.Content`` payload tells ADK to skip the
        agent's body and use our payload as the response. We surface
        the kill reason as JSON so downstream code (e.g. adk_runner's
        verdict extractor) can spot ``verdict='partial'`` cleanly.
        """
        state = callback_context.state
        if not state.get("loop_budget_kill"):
            return None
        # Mark the doer_outcome so adk_runner/git_pr still picks up
        # any partial work that landed on disk this run.
        state.setdefault("feedback_verdict",
                         f"partial loop_budget_kill: "
                         f"{state.get('loop_budget_reason', 'plateau')}")
        try:
            from google.genai import types as gtypes
            payload = json.dumps({
                "verdict": "partial",
                "reason": state.get("loop_budget_reason", "loc_plateau"),
                "loc_history": state.get("loc_history", []),
                "llm_call_count": state.get("llm_call_count", 0),
            })
            return gtypes.Content(
                role="model",
                parts=[gtypes.Part.from_text(text=payload)],
            )
        except Exception as exc:
            log.exception("loop_budget.before_loop_callback failed: %s", exc)
            return None

    return before_loop_callback, after_iteration_callback, before_doer_model_callback


__all__ = [
    "build_loop_budget_callbacks",
    "evaluate_plateau",
    "evaluate_call_budget",
    "reset_call_counters",
]
