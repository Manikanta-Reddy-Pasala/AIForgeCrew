"""gap_eval archetype — research-completeness critic.

Runs AFTER merge_context, BEFORE the Planner. Tool-less single-turn
JSON critic; writes ``gap_verdict``. The graph's ``gap_gate`` reads it
to decide whether to re-dispatch the context fan-out (bounded once).
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "gap_eval"
PROMPT = prompts.GAP_EVAL
OUTPUT_KEY = "gap_verdict"
TOOLS_FACTORY = None


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
