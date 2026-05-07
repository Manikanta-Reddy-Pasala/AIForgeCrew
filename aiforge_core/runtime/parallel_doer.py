"""Concurrent dispatcher for independent child subtickets (option E).

A Plan often emits N leaf subtickets that don't touch each other —
splitting their Doer turns across worktrees lets wall-clock scale with
core count instead of summing serially.

Two safety constraints:

1. **Only run subtickets whose ``scope_allowlist_globs`` are pairwise
   disjoint.** If two subtickets both touch ``aiforge_core/runtime/``,
   the safer path is to serialise them so a later turn isn't reading a
   half-written file. This module only flags the conflict and groups
   compatible tickets into batches; it does not write the worktrees.

2. **Bounded fan-out** via ``max_parallel`` to keep load on the local
   model server sane.

The actual Doer execution stays in ``adk_runner`` — this module is a
pure planner: hand it subtickets, it returns batches.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Subticket:
    id: str
    scope_allowlist_globs: tuple[str, ...]


def _glob_overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Heuristic glob conflict — pairs share a literal prefix.

    KISS: we don't try to evaluate globs against the filesystem. If
    two patterns share any non-empty prefix segment they're treated
    as conflicting. Operators can opt out via empty allowlist (which
    means "no scope constraint" → always conflicts to be safe).
    """
    if not a or not b:
        return True
    # Compare each literal prefix up to the first wildcard.
    def _prefix(p: str) -> str:
        for i, ch in enumerate(p):
            if ch in "*?[":
                return p[:i]
        return p
    pas = {_prefix(p).rstrip("/") for p in a if _prefix(p)}
    pbs = {_prefix(p).rstrip("/") for p in b if _prefix(p)}
    if not pas or not pbs:
        return True
    for x in pas:
        for y in pbs:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def batch(subtickets: list[Subticket],
          max_parallel: int = 3) -> list[list[Subticket]]:
    """Group ``subtickets`` into sequential batches of disjoint scopes.

    Each returned batch is safe to run in parallel; batches must run
    in order. ``max_parallel`` caps batch size to avoid over-loading
    the inference server."""
    if not subtickets:
        return []
    if max_parallel < 1:
        raise ValueError(f"max_parallel must be >= 1, got {max_parallel}")

    batches: list[list[Subticket]] = []
    for st in subtickets:
        placed = False
        for b in batches:
            if len(b) >= max_parallel:
                continue
            if all(not _glob_overlaps(st.scope_allowlist_globs,
                                     other.scope_allowlist_globs)
                   for other in b):
                b.append(st)
                placed = True
                break
        if not placed:
            batches.append([st])
    return batches


__all__ = ["Subticket", "batch"]
