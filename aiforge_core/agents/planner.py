"""Planner archetype — emits plan + child subtickets.

Reads the parent ticket and produces a JSON plan with
``{steps, scope_allowlist_globs, child_subtickets}``. Every test
subticket MUST reference a test skeleton template per the YAML
termination contract.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "planner"
PROMPT = prompts.PLANNER
OUTPUT_KEY = "plan_md"
TOOLS_FACTORY = None   # text-protocol agent — tools come through GA, not ADK


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
