"""ADK SequentialAgent factory for the v6 pipeline.

Pipeline shape::

    SequentialAgent[
        planner,
        verifier,
        LoopAgent[doer, feedback]   # cap = 3 iterations
        learner,
    ]

Each ``LlmAgent`` is wrapped around an :class:`EscalatingLlm` so the
local mlx-lm primary auto-falls-over to the operator's cloud chain
(Ollama Cloud → Anthropic → claude_local) without the orchestrator
having to know about it.

Per-role provider routing comes from
:func:`aiforge_core.config.agent_config.resolve_litellm` + the cloud
chain helper. Prompts live in :mod:`prompts` so this module stays a
straight wiring layer.
"""
from __future__ import annotations

from typing import Any

from aiforge_core.agents.loader import load_agents
from aiforge_core.config import agent_config as _acfg

from . import prompts
from .doer_tools import adk_function_tools as _doer_tools
from .escalating_llm import EscalatingLlm
from .local_probe import maybe_substitute_primary


def build_litellm_model(role: str):
    """Return an :class:`EscalatingLlm` for the given role.

    Wraps the primary (mlx-lm via LiteLlm or ClaudeSubscriptionLlm)
    with the cloud fallback chain so transport / empty-response /
    routing failures get retried transparently. Disable the chain
    with ``AIFORGE_ESCALATE_DISABLE=1``.

    Pre-flight probe: when the operator's profile points at the local
    mlx-lm endpoint and that endpoint is dead, the primary cfg is
    swapped at build time to a cloud default (Ollama Cloud's
    ``qwen3-coder-next``) — see :mod:`local_probe`. Avoids paying the
    primary→fail→cloud round-trip cost on every single Doer turn when
    LM Studio is just off.
    """
    primary = _acfg.resolve_litellm(role)
    primary = maybe_substitute_primary(role, primary)
    chain = _acfg.cloud_escalation_chain(role)
    return EscalatingLlm.build(role, primary, chain)


def build_pipeline():
    """Construct the SequentialAgent. Returns the root agent ready for
    ``Runner(agent=..., session_service=...)``."""
    from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent

    contracts = load_agents()  # parses agents.yaml, validates v6 shape

    def _agent(role: str, instruction: str, output_key: str,
               tools: list | None = None) -> "LlmAgent":
        c = contracts[role]
        kwargs: dict[str, Any] = {
            "name": role,
            "model": build_litellm_model(role),
            "instruction": instruction,
            "output_key": output_key,
            "timeout": c.contract.max_wall_s,
        }
        if tools:
            kwargs["tools"] = tools
        return LlmAgent(**kwargs)

    planner = _agent("planner", prompts.PLANNER, output_key="plan_md")
    verifier = _agent("verifier", prompts.VERIFIER, output_key="verifier_verdict")
    doer = _agent("doer", prompts.DOER, output_key="doer_outcome",
                  tools=_doer_tools())
    feedback = _agent("feedback", prompts.FEEDBACK, output_key="feedback_verdict")
    learner = _agent("learner", prompts.LEARNER, output_key="facts_json")

    doer_loop = LoopAgent(
        name="doer_feedback_loop",
        sub_agents=[doer, feedback],
        max_iterations=3,
    )
    return SequentialAgent(
        name="aiforge_v6_pipeline",
        sub_agents=[planner, verifier, doer_loop, learner],
    )


__all__ = ["build_pipeline", "build_litellm_model"]
