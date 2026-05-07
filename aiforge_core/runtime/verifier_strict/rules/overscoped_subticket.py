"""Reject subtickets that declare too many files.

A subticket touching 5+ files is usually doing two unrelated jobs;
pre-empt the Doer ``scope_violation`` failure mode by rejecting at
plan time instead of mid-loop.
"""
from __future__ import annotations

from .._helpers import subtickets_of, subticket_id

MAX_FILES_PER_SUBTICKET = 5

KIND = "strict_overscoped_subticket"


def rule(plan: dict) -> list[dict]:
    issues: list[dict] = []
    for st in subtickets_of(plan):
        files = st.get("files") or st.get("scope_files") or []
        if isinstance(files, list) and len(files) > MAX_FILES_PER_SUBTICKET:
            issues.append({
                "kind": KIND,
                "message": f"subticket {subticket_id(st)!r} declares "
                           f"{len(files)} files "
                           f"(cap {MAX_FILES_PER_SUBTICKET}); split it",
            })
    return issues


__all__ = ["MAX_FILES_PER_SUBTICKET", "KIND", "rule"]
