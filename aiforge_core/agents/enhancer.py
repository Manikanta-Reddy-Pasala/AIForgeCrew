"""Enhancer archetype — Claude rewrites the operator's raw ticket
body into a structured brief the downstream local Doer can act on.

First stage of the SequentialAgent pipeline. Reads ``ticket.body``
through the shared prompt frame and writes the enriched version to
``state['enhanced_body']``. The Planner pulls from that key when
present (falls back to the raw body otherwise).

KISS in-framework: this is a regular ADK LlmAgent. The model
factory passed in already knows how to force claude_local when the
caller wants Claude pinned for this role (see
``pipeline._claude_pinned_model_factory``).
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "enhancer"
PROMPT = prompts.ENHANCER
OUTPUT_KEY = "enhanced_body"
TOOLS_FACTORY = None  # text-only — no tools at this stage


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
