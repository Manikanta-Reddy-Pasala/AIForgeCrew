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


def _already_polished(state):
    """The refiner polishes a diff once. On a later Doer iteration the diff is a
    fix to something already polished, and a failing run has nothing to polish.
    ``AIFORGE_REFINE_EVERY_ITER=1`` runs it every iteration."""
    import os
    if os.environ.get("AIFORGE_REFINE_EVERY_ITER", "").strip().lower() in (
            "1", "true", "yes", "on"):
        return None
    if int(state.get("doer_iters", 0) or 0) < 1:
        return None
    return {"refiner_skipped": True, "changes": [],
            "rationale": "already refined on an earlier iteration"}


def _small_plan_decide(state):
    """A plan of one small step produces a diff of a few lines: nothing to
    polish that Feedback and the Validator do not already read.
    ``AIFORGE_SMALL_PLAN_SKIP=0`` runs the Refiner on it."""
    from aiforge_core.runtime.graph_pipeline import small_plan_skip
    if not small_plan_skip(state):
        return None
    return {"refiner_skipped": True, "changes": [],
            "rationale": "polish skipped: the plan is one small step"}


def _skip_decide(state):
    return _already_polished(state) or _small_plan_decide(state)


def build(model_factory: _base.ModelFactory):
    agent = _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )
    return _base.skip_agent_when(agent, _skip_decide)


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
