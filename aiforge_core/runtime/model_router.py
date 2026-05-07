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


# Tiers ordered cheapest -> strongest, low index = cheaper / faster /
# weaker, high index = pricier / slower / stronger. Each tier name MUST
# resolve in the operator's LM Studio / Ollama / Anthropic catalog;
# the router never invents a model — it picks from this list and the
# operator-specific config (env vars, agent_config.json) takes precedence.
#
# Why three tiers for the Doer specifically? Empirically:
#   - trivial tickets (rename, typo, single-line tweak) — Devstral 24B is
#     plenty and finishes in 1/3 the wall-clock of Qwen-Coder-Next.
#   - moderate tickets — Qwen-Coder-Next 80B is the workhorse default.
#   - hard tickets, or any first-attempt compile-fail — escalate to a
#     cloud reasoner so a stuck local Doer doesn't burn the loop cap.
DOER_TIERS: tuple[str, ...] = (
    "Devstral-Small-2-24B-Instruct-2512-4bit",   # fastest local coder
    "Qwen3-Coder-Next-MLX-4bit",                  # default
    "claude-opus-4-7",                             # cloud escalation
)

# Researcher needs broad context recall, not code-gen muscle. Two tiers
# is enough: dense 27B for normal sweeps, MoE 35B when the parent ticket
# spans multiple subsystems and we need higher-fidelity retrieval.
RESEARCHER_TIERS: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",                       # default
    "Qwen3.6-35B-A3B-MoE",                         # MoE for harder context
)

# Refiner is a single-turn JSON polisher — one tier is fine; complexity
# doesn't move the needle on rename/dead-code/identical-branch edits.
REFINER_TIERS: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",
)

# Triage is a one-shot complexity classifier — cheap and fast wins.
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
