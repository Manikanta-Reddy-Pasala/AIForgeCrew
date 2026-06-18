"""Prompt-shape tests for the research-gap loop. No ADK import."""
from __future__ import annotations

from aiforge_core.runtime import prompts, prompts_extended


def test_gap_eval_prompt_exported_and_shaped() -> None:
    p = prompts.GAP_EVAL
    assert "sufficient" in p             # the JSON contract field
    assert "{context_brief_md?}" in p    # reads the merged research brief
    assert "{enhanced_body?}" in p       # reads the ticket


def test_researcher_prompt_has_gap_block() -> None:
    assert "{research_gap_brief_md?}" in prompts_extended.RESEARCHER
