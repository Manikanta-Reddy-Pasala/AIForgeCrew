"""Reject plans whose subticket count exceeds the empirical cap.

A plan with 9+ subtickets almost always fragments work that should
belong to a single coherent change; splitting across multiple parent
tickets gives the human reviewer a chance to push back BEFORE the
Doer loop burns turns on a misshapen plan.
"""
from __future__ import annotations

from .._helpers import subtickets_of

MAX_SUBTICKETS = 8

KIND = "strict_too_many_subtickets"


def rule(plan: dict) -> list[dict]:
    sts = subtickets_of(plan)
    if len(sts) <= MAX_SUBTICKETS:
        return []
    return [{
        "kind": KIND,
        "message": f"plan has {len(sts)} subtickets (cap {MAX_SUBTICKETS}); "
                   "split into multiple parent tickets",
    }]


__all__ = ["MAX_SUBTICKETS", "KIND", "rule"]
