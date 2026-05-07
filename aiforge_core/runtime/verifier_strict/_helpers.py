"""Tiny shared helpers for the strict-rule modules.

Centralising the ``child_subtickets`` extraction means a Planner
schema change (e.g. renaming the field) is a one-file edit instead
of touching every rule. Keep this module dependency-free — rules
import from here, not the other way round.
"""
from __future__ import annotations


def subtickets_of(plan: dict) -> list[dict]:
    """Return ``plan['child_subtickets']`` filtered to dict entries."""
    return [s for s in (plan.get("child_subtickets") or []) if isinstance(s, dict)]


def subticket_id(st: dict) -> str:
    """Best-effort identifier for a subticket — fall back gracefully."""
    return st.get("id") or st.get("subticket_id") or "(unnamed)"


__all__ = ["subtickets_of", "subticket_id"]
