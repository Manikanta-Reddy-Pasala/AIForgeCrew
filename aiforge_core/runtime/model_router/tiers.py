"""Per-role model tier lists.

Tiers ordered cheapest -> strongest, low index = cheaper / faster /
weaker, high index = pricier / slower / stronger. Each tier name MUST
resolve in the operator's LM Studio / Ollama / Anthropic catalog;
the router never invents a model — it picks from this list and the
operator-specific config (env vars, agent_config.json) takes precedence.

Why these specific tier counts:

* **Doer (3 tiers)** — empirically distinct routing per complexity.
    - trivial tickets (rename, typo, single-line tweak) — Devstral 24B
      is plenty and finishes in 1/3 the wall-clock of Qwen-Coder-Next.
    - moderate tickets — Qwen-Coder-Next 80B is the workhorse default.
    - hard tickets, or any first-attempt compile-fail — escalate to a
      cloud reasoner so a stuck local Doer doesn't burn the loop cap.
* **Researcher (2 tiers)** — broad context recall, not code muscle.
    Dense 27B for normal sweeps, MoE 35B when the parent ticket spans
    multiple subsystems and we need higher-fidelity retrieval.
* **Refiner (1 tier)** — single-turn JSON polisher. Complexity doesn't
    move the needle on rename/dead-code/identical-branch edits.
* **Triage (1 tier)** — one-shot complexity classifier; cheap wins.
"""
from __future__ import annotations

DOER: tuple[str, ...] = (
    "Devstral-Small-2-24B-Instruct-2512-4bit",   # fastest local coder
    "Qwen3-Coder-Next-MLX-4bit",                  # default
    "claude-opus-4-7",                             # cloud escalation
)

RESEARCHER: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",                       # default
    "Qwen3.6-35B-A3B-MoE",                         # MoE for harder context
)

REFINER: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",
)

TRIAGE: tuple[str, ...] = (
    "Qwen3.6-27B-MLX-4bit",                       # cheap, single-turn JSON
)


def for_role(role: str) -> tuple[str, ...]:
    """Return the tier tuple for ``role`` or ``()`` for unknown roles.

    KISS: a single map kept inline, not a class — adding a role is one
    new top-level constant + one entry here.
    """
    return {
        "doer":       DOER,
        "researcher": RESEARCHER,
        "refiner":    REFINER,
        "triage":     TRIAGE,
    }.get(role, ())


__all__ = ["DOER", "RESEARCHER", "REFINER", "TRIAGE", "for_role"]
