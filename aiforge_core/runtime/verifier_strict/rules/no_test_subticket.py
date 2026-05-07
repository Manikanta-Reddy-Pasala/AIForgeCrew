"""Reject plans with no test subticket.

Every acceptance criterion must come with at least one test subticket,
per the Planner termination contract in ``agents.yaml``. We pattern-
match on ``id``, ``title``, and an explicit ``kind: test`` field so the
rule survives shifts in how the Planner labels test work.
"""
from __future__ import annotations

from .._helpers import subtickets_of

KIND = "strict_no_test_subticket"


def _looks_like_test(st: dict) -> bool:
    return (
        "test" in (st.get("id") or "").lower()
        or "test" in (st.get("title") or "").lower()
        or st.get("kind") == "test"
    )


def rule(plan: dict) -> list[dict]:
    sts = subtickets_of(plan)
    if not sts:
        # Empty plan is a separate concern — caught by other rules.
        return []
    if any(_looks_like_test(st) for st in sts):
        return []
    return [{
        "kind": KIND,
        "message": "no test subticket found — every plan must include "
                   "at least one test subticket per acceptance criterion",
    }]


__all__ = ["KIND", "rule"]
