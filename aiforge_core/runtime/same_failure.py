"""One loop rule for every repair loop: the same failure, fix after fix.

A healthy run may take as many steps and as much time as it needs. What is
never healthy is a failure that survives fix after fix: the model edits
something different each round, the workspace keeps changing, every
"progress" signal fires — and the same test fails the same way. That is the
loop this module detects, keyed on the failure (see
:mod:`aiforge_core.runtime.failure_signature`), not on the command, so
``pytest -x`` and ``pytest -q`` count as one.

The rule: the same non-empty signature seen in ``AIFORGE_SAME_FAILURE_LIMIT``
(default 3) distinct workspace states — or twice that many times in all —
without its failure count dropping. The first trip earns a directed nudge;
the budget for that nudge never refills, so the next trip stops the loop.

The tracker is a plain dict of lists and ints, so a pipeline can keep it in
its session state.
"""
from __future__ import annotations

import os

from aiforge_core.runtime.failure_signature import Failure

#: Signatures remembered per tracker; the oldest are forgotten.
_MAX_SIGS = 64

NUDGE = "nudge"
STOP = "stop"


def same_failure_limit() -> int:
    """Distinct workspace states one failure may survive
    (``AIFORGE_SAME_FAILURE_LIMIT``, default 3, at least 2)."""
    try:
        val = int(os.environ.get("AIFORGE_SAME_FAILURE_LIMIT", "3"))
    except ValueError:
        return 3
    return max(2, val)


def observe(track: dict, fail: Failure, state_key: str, limit: int | None = None) -> str:
    """Record one sighting of ``fail`` in workspace state ``state_key``.

    Returns ``""`` (fine), ``"nudge"`` (the first trip) or ``"stop"`` (a trip
    after the nudge was spent). A failure count lower than the best seen for
    this signature is progress and starts its count over."""
    if not fail or not fail.signature:
        return ""
    limit = limit or same_failure_limit()
    sigs = track.setdefault("sigs", {})
    entry = sigs.pop(fail.signature, None)
    if entry is None or fail.count < entry.get("best", fail.count):
        entry = {"states": [], "seen": 0, "best": fail.count}
    if state_key not in entry["states"]:
        entry["states"] = (entry["states"] + [state_key])[-2 * limit:]
    entry["seen"] += 1
    sigs[fail.signature] = entry               # most recent last
    while len(sigs) > _MAX_SIGS:
        sigs.pop(next(iter(sigs)))
    if len(entry["states"]) < limit and entry["seen"] < 2 * limit:
        return ""
    # Tripped. The model gets a fresh count to act on the nudge; the nudge
    # itself is spent for the whole run.
    entry["states"], entry["seen"] = [], 0
    track["trips"] = int(track.get("trips", 0)) + 1
    track["last"] = fail.headline or fail.signature[:200]
    return NUDGE if track["trips"] == 1 else STOP


def nudge_text(fail: Failure, limit: int | None = None) -> str:
    """The directed nudge after the first trip."""
    n = limit or same_failure_limit()
    what = fail.headline or fail.signature[:200]
    return ("[loop guard — not the user] The SAME failure survived "
            f"{n} different fixes: {what}. Your fixes are missing its cause. "
            "Before touching code again: say in one or two sentences WHY the "
            "last fixes did not change this failure (re-read the failing "
            "test and the code it calls, not your own patch). Then change "
            "approach — or, if you cannot tell what the failure needs, stop "
            "and ask the user.")


def stop_text(fail_or_headline) -> str:
    """What the user reads when the loop stops for this reason."""
    what = (fail_or_headline.headline or fail_or_headline.signature[:200]
            if isinstance(fail_or_headline, Failure) else str(fail_or_headline or ""))
    return ("I keep hitting the same failure after several different fixes"
            + (f" ({what})" if what else "")
            + ". I've paused rather than keep trying — could you take a look "
              "or tell me how you'd like me to proceed?")


__all__ = ["NUDGE", "STOP", "same_failure_limit", "observe", "nudge_text",
           "stop_text"]
