"""Feedback archetype — post-execution judge.

Single-turn JSON ``{verdict: pass|fail|scope_violation, rationale}``.
``scope_violation`` outranks ``fail`` per the YAML rule: any write
outside the allowlist is a scope_violation regardless of test colour.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "feedback"
PROMPT = prompts.FEEDBACK
OUTPUT_KEY = "feedback_verdict"
TOOLS_FACTORY = None   # judge — tools forbidden by contract


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
