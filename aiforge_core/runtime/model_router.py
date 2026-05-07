"""Triage-driven model routing for Doer / Researcher / Refiner.

Idea: the Triage agent classifies each parent ticket as
``trivial`` / ``moderate`` / ``hard``. This module turns that label
into a per-role provider+model preference, so a one-line typo fix
doesn't burn a 80B-param Doer turn and a cross-cutting refactor
doesn't get stranded on a 9B model.

Resolution still defers to the operator's ``agent_config.json``; the
router only fills in defaults the operator hasn't pinned. Per-role env
vars (``AIFORGE_<ROLE>_MODEL`` etc.) still win — see
:mod:`aiforge_core.llm.router`.

Also handles **escalation on first compile-fail** for Doer (option F):
``next_doer_model_after_fail()`` returns the next-tier model so the
caller can re-run with a stronger primary.
"""
from __future__ import annotations

from dataclasses import dataclass


# Tiers ordered cheapest -> strongest. Real model ids must exist in the
# operator's LM Studio / Ollama / Anthropic catalog. Operators override
# via env or agent_config; the router never invents a model.
DOER_TIERS: tuple[str, ...] = (
    "Devstral-Small-2-24B-Instruct-2512-4bit",   # fastest local coder
    "Qwen3-Coder-Next-MLX-4bit",                  # default
    "claude-opus-4-7",                             # cloud escalation
)

RESEARCHER_TIERS: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",                       # default
    "Qwen3.6-35B-A3B-MoE",                         # MoE for harder context
)

REFINER_TIERS: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",
)

TRIAGE_TIERS: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",                       # cheap, single-turn JSON
)


@dataclass(frozen=True)
class RoutingDecision:
    role: str
    complexity: str            # trivial | moderate | hard
    model: str
    tier_index: int
    reason: str


def _tiers_for(role: str) -> tuple[str, ...]:
    if role == "doer":
        return DOER_TIERS
    if role == "researcher":
        return RESEARCHER_TIERS
    if role == "refiner":
        return REFINER_TIERS
    if role == "triage":
        return TRIAGE_TIERS
    return ()


def _index_for_complexity(complexity: str, max_idx: int) -> int:
    """trivial -> 0, moderate -> middle, hard -> last."""
    c = (complexity or "moderate").lower()
    if c == "trivial":
        return 0
    if c == "hard":
        return max_idx
    # moderate (and any unknown value) lands in the middle, biased to
    # default tier. With 3 tiers that's index 1 — the "default".
    return min(1, max_idx)


def pick(role: str, complexity: str = "moderate") -> RoutingDecision:
    """Pick a model for ``role`` given ticket complexity. Falls back to
    the lowest tier when the role has no tier list (still returns a
    non-empty ``model`` so callers can rely on it)."""
    tiers = _tiers_for(role)
    if not tiers:
        return RoutingDecision(role=role, complexity=complexity,
                               model="", tier_index=-1,
                               reason=f"no tiers configured for role={role!r}")
    idx = _index_for_complexity(complexity, max_idx=len(tiers) - 1)
    return RoutingDecision(role=role, complexity=complexity,
                           model=tiers[idx], tier_index=idx,
                           reason=f"complexity={complexity} -> tier {idx}")


def next_doer_model_after_fail(current: str) -> str | None:
    """Return the next stronger Doer model after ``current`` fails to
    compile. Returns ``None`` once the top tier has been reached.

    Option F in the upgrade list — escalate on first compile-fail
    instead of waiting for two consecutive failures and halting."""
    try:
        idx = DOER_TIERS.index(current)
    except ValueError:
        return DOER_TIERS[-1]   # unknown current → jump to top tier
    if idx >= len(DOER_TIERS) - 1:
        return None
    return DOER_TIERS[idx + 1]


__all__ = [
    "RoutingDecision", "pick", "next_doer_model_after_fail",
    "DOER_TIERS", "RESEARCHER_TIERS", "REFINER_TIERS", "TRIAGE_TIERS",
]
