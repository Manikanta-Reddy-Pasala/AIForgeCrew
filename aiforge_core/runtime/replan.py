"""REPLAN signal helper (gap A2).

The Doer loop (``LoopAgent[doer, refiner, feedback]``) retries the SAME
Doer up to ``max_iterations`` times. When the loop exhausts iterations
still failing, blindly retrying the Doer rarely helps — the plan itself
is usually too broad or wrong. This module provides two pure helpers:

* :func:`should_replan` — True when the last ``max_fail`` Feedback
  verdicts are all ``"fail"``.
* :func:`build_replan_note` — a short instruction string the pipeline
  stashes into session state (``replan_note``) so a subsequent Planner
  invocation / next ticket attempt can narrow scope and re-plan smaller.

Both are side-effect free so they stay trivially testable; the pipeline
wires the signal + state key + trace emit around them.
"""
from __future__ import annotations


def should_replan(verdicts: list[str], *, max_fail: int = 2) -> bool:
    """Return True when the last ``max_fail`` verdicts are all ``"fail"``.

    Args:
      verdicts: ordered Feedback verdicts (``"pass"`` / ``"fail"`` / ...).
      max_fail: how many trailing consecutive fails trigger a replan.
        Must be >= 1; non-positive values never trigger.

    Empty history (or fewer verdicts than ``max_fail``) returns False.
    """
    if max_fail < 1:
        return False
    if len(verdicts) < max_fail:
        return False
    tail = verdicts[-max_fail:]
    return all(v == "fail" for v in tail)


def build_replan_note(verdicts: list[str], last_rationale: str) -> str:
    """Build a short replan instruction for the next Planner attempt.

    Mentions how many attempts failed and the most recent rationale, then
    asks the Planner to narrow scope and re-plan smaller.
    """
    n = len(verdicts)
    rationale = (last_rationale or "no rationale recorded").strip()
    return (
        f"previous {n} attempt(s) failed: {rationale}; "
        "narrow scope, re-plan smaller — break the work into the single "
        "smallest change that moves Feedback toward pass."
    )


__all__ = ["should_replan", "build_replan_note"]
