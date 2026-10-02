"""Validator archetype — final pre-PR sanity gate.

Last stage of the SequentialAgent pipeline (after Learner). Reads
the session state populated by Doer / Feedback / Refiner and emits
a structured JSON verdict at ``state['validator_verdict']``. The
runner reads that field after the pipeline exits and folds it into
``ticket.metadata.validator_*`` so operators see both the in-loop
verdict and the validator's independent take.

KISS in-framework: regular ADK LlmAgent built with the shared
``pipeline.build_litellm_model`` factory — the operator's configured
model for this role plus the cloud escalation chain.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "validator"
PROMPT = prompts.VALIDATOR
OUTPUT_KEY = "validator_verdict"
TOOLS_FACTORY = None  # judgment only — Validator never edits


def _green_decide(state):
    """Feedback passed, the tests ran and are green, and nothing is known broken
    or unfinished: the independent take would only repeat that. Skipped, with a
    verdict that says why. ``AIFORGE_VALIDATE_ON_GREEN=1`` always runs it."""
    import os
    if os.environ.get("AIFORGE_VALIDATE_ON_GREEN", "").strip().lower() in (
            "1", "true", "yes", "on"):
        return None
    from aiforge_core.runtime.graph_pipeline._parsers import _feedback_passed
    if not _feedback_passed(state):
        return None
    if state.get("tests_ok") is not True:
        return None
    if state.get("typecheck_ok") is False or state.get("lint_ok") is False:
        return None
    if state.get("doer_incomplete") or state.get("quality_issue"):
        return None
    return {"verdict": "approve", "skipped": True,
            "rationale": "feedback passed and the tests are green"}


def build(model_factory: _base.ModelFactory):
    agent = _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )
    return _base.skip_agent_when(agent, _green_decide)


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
