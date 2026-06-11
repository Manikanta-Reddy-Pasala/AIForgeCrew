"""Verifier archetype — single-turn plan critic.

LEGACY: superseded in the Workflow graph by the parallel
verify_correctness / verify_scope / verify_risk trio (see
``runtime.parallel_stages``); kept registered for back-compat with
callers that build a one-shot verifier directly.

The model returns ``{verdict: pass|reject, issues, rationale}``.
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
