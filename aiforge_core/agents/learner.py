"""Learner archetype — fact distiller.

Runs ONLY when ``feedback_verdict == "pass"``. Emits a strict JSON
array of ``:Fact`` candidates. The server-side ADK ``write_fact``
plugin persists them after schema validation — the model itself has
no write tool, which is why ``allowed`` is empty in the YAML.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "learner"
PROMPT = prompts.LEARNER
OUTPUT_KEY = "facts_json"
TOOLS_FACTORY = None   # write_fact is server-side, not a model tool


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
