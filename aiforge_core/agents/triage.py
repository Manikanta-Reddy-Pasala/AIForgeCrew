"""Triage archetype — single-turn complexity classifier.

Runs FIRST (in the orchestration layer, upstream of the ADK
SequentialAgent) so its ``complexity`` verdict can steer the
trivial-fast-path routing in ``runtime.graph_pipeline``. Tool-less by
contract — see ``agents.yaml`` ``triage.tools.allowed: []``.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts_extended

from . import _base

ROLE = "triage"
PROMPT = prompts_extended.TRIAGE
OUTPUT_KEY = "triage_verdict"
TOOLS_FACTORY = None   # tool-less single-turn JSON classifier


def build(model_factory: _base.ModelFactory):
    """Construct the triage ``LlmAgent``."""
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
