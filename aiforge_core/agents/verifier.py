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


def _reverify_decide(state):
    """The replan budget is spent: the gate proceeds to the Doer whatever this
    verdict says (``_verifier_gate``), so the second model call changes
    nothing. Skip it and leave a verdict that says why."""
    from aiforge_core.runtime.graph_pipeline._config import MAX_VERIFY_REPLANS
    if int(state.get("verify_replan_count", 0) or 0) < MAX_VERIFY_REPLANS:
        return None
    return {"verdict": "pass", "skipped": True,
            "rationale": "re-verification skipped: replan budget used"}


def _small_plan_decide(state):
    """The Planner kept its own plan to one small step (no subtickets, a couple
    of named files): there is no decomposition, scope spread or migration for
    a plan critic to judge, and the work is still checked after the Doer
    (tests, Feedback, Validator). ``AIFORGE_SMALL_PLAN_SKIP=0`` runs it."""
    from aiforge_core.runtime.graph_pipeline import small_plan_skip
    if not small_plan_skip(state):
        return None
    return {"verdict": "pass", "skipped": True,
            "rationale": "plan check skipped: the plan is one small step"}


def _skip_decide(state):
    return _reverify_decide(state) or _small_plan_decide(state)


def build(model_factory: _base.ModelFactory):
    agent = _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )
    return _base.skip_agent_when(agent, _skip_decide)


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
