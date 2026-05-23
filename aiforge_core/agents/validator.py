"""Validator archetype — Claude's final pre-PR sanity gate.

Last stage of the SequentialAgent pipeline (after Learner). Reads
the session state populated by Doer / Feedback / Refiner and emits
a structured JSON verdict at ``state['validator_verdict']``. The
runner reads that field after the pipeline exits and folds it into
``ticket.metadata.validator_*`` so operators see both the in-loop
verdict and Claude's independent take.

KISS in-framework: regular LlmAgent, claude_local pinned via the
pipeline's per-role model factory wrapper.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "validator"
PROMPT = prompts.VALIDATOR
OUTPUT_KEY = "validator_verdict"
TOOLS_FACTORY = None  # judgment only — Validator never edits


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
