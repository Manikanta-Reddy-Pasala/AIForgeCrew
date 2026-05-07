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


def _literal_prefix(glob: str) -> str:
    """Return the part of ``glob`` before the first wildcard.

    For ``aiforge_core/runtime/**`` returns ``aiforge_core/runtime/``.
    For ``src/foo.py`` (no wildcards) returns the whole string. The
    trailing slash is significant — see :func:`_prefix_conflict`.
    """
    for i, ch in enumerate(glob):
        if ch in "*?[":
            return glob[:i]
    return glob


def _prefix_conflict(x: str, y: str) -> bool:
    """True when one literal-prefix is a directory-prefix of the other.

    Two scopes conflict when their writable regions overlap. We treat
    them as overlapping if the path-prefixes are identical OR one is a
    parent dir of the other (``aiforge_core/`` vs ``aiforge_core/runtime/``).
    Different files in the same directory are NOT a conflict — the
    Doers can edit ``foo.py`` and ``bar.py`` in parallel safely.
    """
    return (
        x == y
        or x.startswith(y + "/")
        or y.startswith(x + "/")
    )


def _glob_overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Heuristic conflict check between two scope_allowlist_globs.

    KISS: we don't evaluate globs against the filesystem. We reduce
    each glob to its literal prefix, strip trailing slashes for clean
    comparison, then check whether ANY prefix from set ``a`` shares a
    directory-prefix relationship with ANY prefix in set ``b``.

    An empty allowlist means "no scope constraint" — that's strictly
    less safe than any constrained scope, so we always treat it as
    conflicting to avoid running an unscoped Doer alongside anything.
    """
    if not a or not b:
        return True

    pas = {_literal_prefix(p).rstrip("/") for p in a if _literal_prefix(p)}
    pbs = {_literal_prefix(p).rstrip("/") for p in b if _literal_prefix(p)}
    if not pas or not pbs:
        # Every glob in at least one set was a pure wildcard like ``**``.
        # That's a "match everything" pattern — treat as conflict.
        return True

    return any(_prefix_conflict(x, y) for x in pas for y in pbs)


def _can_join(st: Subticket, batch: list[Subticket],
              max_parallel: int) -> bool:
    """True when ``st`` can be added to ``batch`` without scope conflicts.

    A subticket joins a batch only when (a) the batch isn't already at
    its parallel cap and (b) none of its existing members share a
    scope-prefix with the candidate. Pulled out as a helper to make the
    placement logic in :func:`batch` linear and readable.
    """
    if len(batch) >= max_parallel:
        return False
    return all(
        not _glob_overlaps(st.scope_allowlist_globs, other.scope_allowlist_globs)
        for other in batch
    )


def batch(subtickets: list[Subticket],
          max_parallel: int = 3) -> list[list[Subticket]]:
    """Group ``subtickets`` into sequential batches of disjoint scopes.

    Each returned batch is safe to run in parallel; batches MUST run
    in order. The first batch that can absorb a subticket wins — this
    is a greedy first-fit, intentional KISS over a perfect bin-pack
    that nobody asked for.

    Args:
      subtickets:    Plan-emitted leaves, in their original order.
      max_parallel:  Per-batch cap. Tighter caps trade throughput for
        a calmer inference server; default 3 matches the LM Studio
        ``parallel`` slot count we ship with.
    """
    if not subtickets:
        return []
    if max_parallel < 1:
        raise ValueError(f"max_parallel must be >= 1, got {max_parallel}")

    batches: list[list[Subticket]] = []
    for st in subtickets:
        placed = False
        for b in batches:
            if _can_join(st, b, max_parallel):
                b.append(st)
                placed = True
                break
        if not placed:
            batches.append([st])
    return batches


__all__ = ["Subticket", "batch"]
