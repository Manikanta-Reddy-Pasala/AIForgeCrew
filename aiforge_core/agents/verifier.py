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


def _reverify_skip(*, callback_context, **_kw):
    """The replan budget is spent: the gate proceeds to the Doer whatever this
    verdict says (``_verifier_gate``), so the second model call changes
    nothing. Skip it and leave a verdict that says why."""
    from aiforge_core.runtime.graph_pipeline._config import MAX_VERIFY_REPLANS
    state = callback_context.state
    if int(state.get("verify_replan_count", 0) or 0) < MAX_VERIFY_REPLANS:
        return None
    verdict = {"verdict": "pass", "skipped": True,
               "rationale": "re-verification skipped: replan budget used"}
    state[OUTPUT_KEY] = verdict
    from google.genai import types
    import json
    return types.Content(role="model", parts=[types.Part(text=json.dumps(verdict))])


def _chain(first, second):
    def _cb(*, callback_context, **kw):
        out = first(callback_context=callback_context, **kw)
        return out if out is not None else second(callback_context=callback_context, **kw)
    return _cb


def build(model_factory: _base.ModelFactory):
    agent = _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )
    stage = agent.before_agent_callback
    agent.before_agent_callback = (
        _chain(_reverify_skip, stage) if callable(stage) else _reverify_skip)
    return agent


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
