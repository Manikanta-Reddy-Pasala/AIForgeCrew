"""Verifier archetype — single-turn plan critic.

The model returns ``{verdict: pass|reject, issues, rationale}``. The
orchestrator layers ``runtime.verifier_strict.apply`` on top to add
deterministic structural rules (cap on subticket count etc.). Any
strict-mode rejection flips the verdict regardless of what the model
said — this is intentional: an LLM that rubber-stamps every plan is
worse than no verifier at all.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "verifier"
PROMPT = prompts.VERIFIER
OUTPUT_KEY = "verifier_verdict"
TOOLS_FACTORY = None   # judge — no tool calls allowed


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
