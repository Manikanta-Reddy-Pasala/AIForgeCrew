"""Refiner archetype — behaviour-neutral diff polish.

Runs after Doer, before Feedback. Tool-less single-turn JSON: the
prompt enumerates allowed (rename, dead-code drop, identical-branch
merge) and forbidden (signature change, file move, format-only)
edits. ``refiner_skipped=true`` is the model's escape hatch when the
diff is already clean.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts_extended

from . import _base

ROLE = "refiner"
PROMPT = prompts_extended.REFINER
OUTPUT_KEY = "refiner_changes"
TOOLS_FACTORY = None   # judge-style — applies are orchestrator-side


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
