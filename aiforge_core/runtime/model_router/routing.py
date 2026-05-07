"""Complexity → tier resolution.

Single concern: given a role + complexity label, return the model id
the orchestrator should request. This module owns NO escalation logic
— that lives in :mod:`.escalation` so a compile-fail rerun doesn't
have to mirror the trivial/moderate/hard mapping.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import tiers


@dataclass(frozen=True)
class RoutingDecision:
    role: str
    complexity: str            # trivial | moderate | hard
    model: str
    tier_index: int
    reason: str


def _index_for_complexity(complexity: str, max_idx: int) -> int:
    """Map a complexity label onto a tier index.

    trivial -> 0 (cheapest), hard -> max_idx (strongest).
    moderate (and any unknown / typo'd value) lands at index 1 when the
    tier list has at least three entries — that's the canonical
    "default" slot. For shorter tier lists we clamp to ``max_idx`` so
    we never overshoot.
    """
    c = (complexity or "moderate").lower()
    if c == "trivial":
        return 0
    if c == "hard":
        return max_idx
    return min(1, max_idx)


def pick(role: str, complexity: str = "moderate") -> RoutingDecision:
    """Pick a model for ``role`` given ticket complexity.

    Falls back to an empty model id when the role has no tier list —
    callers can rely on the return shape regardless. Operator overrides
    via ``AIFORGE_<ROLE>_MODEL`` env or ``agent_config.json`` win
    upstream of this function (see :mod:`aiforge_core.llm.router`).
    """
    role_tiers = tiers.for_role(role)
    if not role_tiers:
        return RoutingDecision(
            role=role, complexity=complexity, model="",
            tier_index=-1, reason=f"no tiers configured for role={role!r}",
        )
    idx = _index_for_complexity(complexity, max_idx=len(role_tiers) - 1)
    return RoutingDecision(
        role=role, complexity=complexity, model=role_tiers[idx],
        tier_index=idx, reason=f"complexity={complexity} -> tier {idx}",
    )


__all__ = ["RoutingDecision", "pick"]
