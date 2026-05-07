"""Learner prompt — fact distillation, only on verdict=pass.

When ``feedback_verdict.verdict != 'pass'`` the orchestrator skips
this archetype entirely; the prompt's empty-list fallback is a
defence-in-depth for the case where the orchestrator runs Learner by
mistake.
"""
from __future__ import annotations

PROMPT = (
    "You are the Learner. ONLY when state['feedback_verdict'].verdict "
    "== 'pass', emit JSON facts_json: "
    "[{text, about: [path|fqn|ticket], tags}]. Otherwise emit []."
)

__all__ = ["PROMPT"]
