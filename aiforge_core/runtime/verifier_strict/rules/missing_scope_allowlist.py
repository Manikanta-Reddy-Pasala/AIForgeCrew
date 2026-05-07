"""Reject subtickets with empty ``scope_allowlist_globs``.

The Doer's ScopeGuard derives its writable region from this list. An
empty allowlist means "no scope constraint", which is strictly less
safe than any constrained scope and historically leads to surprise
file edits — flag it at plan time.
"""
from __future__ import annotations

from .._helpers import subtickets_of, subticket_id

KIND = "strict_missing_scope_allowlist"


def rule(plan: dict) -> list[dict]:
    issues: list[dict] = []
    for st in subtickets_of(plan):
        if not st.get("scope_allowlist_globs"):
            issues.append({
                "kind": KIND,
                "message": (f"subticket {subticket_id(st)!r} has empty "
                            "scope_allowlist_globs"),
            })
    return issues


__all__ = ["KIND", "rule"]
