"""LOC-delta kill switch for the Doer/Refiner/Feedback LoopAgent.

The LoopAgent caps iterations at 3 by default, but ONE-117 showed
that even a small cap doesn't bound wall-clock when each iteration
calls dozens of tools without making real progress. The Doer would
spend 35+ minutes polishing a near-final scaffold — file_writes that
edited a single line, then reverted it next turn — without ever
satisfying Feedback.

This module supplies two ADK ``LlmAgent`` callbacks that together
implement a LOC-plateau watchdog:

* ``after_loop_iteration_callback`` — attaches to the Refiner. Reads
  ``state['doer_outcome']['file_diffs']`` (Doer-emitted), computes
  the iteration's LOC count, appends to ``state['loc_history']``.
  When 3 consecutive iterations show |delta| < 50 LOC AND elapsed
  time > 600s, sets ``state['loop_budget_kill']=True``.
* ``before_loop_callback`` — attaches to the LoopAgent. Reads the
  kill flag and, if set, yields a verdict-partial Content that
  short-circuits the loop. Doer's existing commit path then takes
  the partial work to a PR for human review.

Env knobs (all optional):
  - ``AIFORGE_LOOP_LOC_PLATEAU_TURNS``   default 3
  - ``AIFORGE_LOOP_LOC_PLATEAU_DELTA``   default 50
  - ``AIFORGE_LOOP_MIN_ELAPSED_S``       default 600
  - ``AIFORGE_LOOP_BUDGET_DISABLE=1``    disables the watchdog

State shape this module owns (all optional, all created lazily):

    state['loc_history']     -> list[int]    # cumulative LOC per turn
    state['loc_first_seen']  -> float        # epoch seconds of turn 0
    state['loop_budget_kill']-> bool         # set when plateau hits
    state['loop_budget_reason'] -> str       # short tag for traces
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


def _worktree_loc() -> "int | None":
    """Lines changed in the working tree, from git. ``None`` when git cannot
    answer (no repo, no binary, an error).

    This is the REAL progress signal. The fallback below counts file_diffs
    entries, and the Doer's own prompt contract emits
    ``{path, action}`` — no ``content``, no ``loc`` — so that count was the
    number of FILES TOUCHED, typically 1-6. The plateau rule ("three
    consecutive deltas under 50 lines") is then true of every possible turn,
    which turned a progress watchdog into a plain 10-minute timer that shipped
    productive work as ``partial``: 14 new files across four turns read as a
    stall.
    """
    import subprocess
    root = (os.environ.get("AIFORGE_REPO_ROOT") or "").strip()
    if not root:
        try:
            from aiforge_core.runtime import request_context
            root = request_context.get_repo_root() or ""
        except Exception:  # noqa: BLE001
            root = ""
    if not root or not os.path.isdir(root):
        return None
    try:
        out = subprocess.run(                      # noqa: S603 — fixed argv
            ["git", "diff", "--numstat", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return None
        total = 0
        for line in (out.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            for n in parts[:2]:
                if n.isdigit():                    # "-" for binary files
                    total += int(n)
        return total
    except Exception:  # noqa: BLE001 — a watchdog must never break the run
        return None


def _loc_for_turn(state: dict) -> "int | None":
    """Cumulative LOC for the most recent Doer turn, or ``None`` when it
    cannot be measured.

    ``None`` matters: a watchdog that cannot see progress must not conclude
    there is none. Before, an unmeasurable turn counted as "1" and three of
    them looked exactly like a stall.
    """
    outcome = _coerce_doer_outcome(state.get("doer_outcome"))
    if not outcome:
        return 0
    diffs = outcome.get("file_diffs") or []
    if not isinstance(diffs, list):
        return 0
    # Ask GIT first — it knows what actually changed, whatever the model chose
    # to report. Only when git cannot answer do we fall back to the model's
    # own accounting.
    if any(isinstance(e, dict) and not isinstance(e.get("loc"), int)
           and not isinstance(e.get("content"), str) for e in diffs):
        wt = _worktree_loc()
        if wt is not None:
            return wt
        return None            # unmeasurable — NOT "no progress"
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
    # NOT setdefault: a replan clears this key by setting it to None
    # (ADK State has no pop) and setdefault no-ops on an existing-but-
    # None key — the later ``now - None`` then TypeErrors every Refiner
    # turn and the plateau watchdog dies silently for the whole pass.
    first = state.get("loc_first_seen")
    if not isinstance(first, (int, float)):
        state["loc_first_seen"] = now if now is not None else time.time()


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
    if loc is None:
        # Progress could not be MEASURED this turn. A watchdog that cannot see
        # progress must not rule that there was none: recording a placeholder
        # made three unmeasurable turns indistinguishable from a stall, which
        # is how productive work got shipped as `partial`.
        log.debug("loop_budget: turn not measurable — no plateau judgement")
        return False
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


def build_loop_budget_callbacks() -> tuple[
    Callable[..., Any] | None,
    Callable[..., Any] | None,
]:
    """Wire the plateau watcher into the LoopAgent.

    Returns ``(before_loop_callback, after_iteration_callback)``.
    Both are ``None`` when the watchdog is env-disabled — caller
    can fall back to the unguarded shape with a single ``if``.

    The before-loop callback short-circuits the LoopAgent when the
    kill flag is set (next iteration never starts). The after-
    iteration callback is the one that observes Doer LOC output and
    flips the flag — attach it to the Refiner so it runs ONCE per
    loop iteration, after the Doer has updated state.
    """
    if os.environ.get("AIFORGE_LOOP_BUDGET_DISABLE", "0") in ("1", "true"):
        return None, None

    plateau_turns = _env_int("AIFORGE_LOOP_LOC_PLATEAU_TURNS",
                             _DEFAULT_PLATEAU_TURNS)
    plateau_delta = _env_int("AIFORGE_LOOP_LOC_PLATEAU_DELTA",
                             _DEFAULT_PLATEAU_DELTA)
    min_elapsed_s = _env_float("AIFORGE_LOOP_MIN_ELAPSED_S",
                               _DEFAULT_MIN_ELAPSED_S)

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
            })
            return gtypes.Content(
                role="model",
                parts=[gtypes.Part.from_text(text=payload)],
            )
        except Exception as exc:
            log.exception("loop_budget.before_loop_callback failed: %s", exc)
            return None

    return before_loop_callback, after_iteration_callback


__all__ = [
    "build_loop_budget_callbacks",
    "evaluate_plateau",
]
