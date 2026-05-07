"""ADK SequentialAgent factory for the v6 pipeline.

Pipeline shape (extended)::

    SequentialAgent[
        planner,
        verifier,
        researcher,                                   # option A — context brief
        LoopAgent[doer, refiner, feedback]            # cap = 3 iterations
        learner,
    ]

Triage (option G) runs in the orchestration layer BEFORE this pipeline so
its complexity verdict can drive model_router for downstream archetypes.
The Refiner sits between Doer and Feedback so behaviour-neutral polish
doesn't get mistaken for a correctness fix on Feedback re-loops.

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

from . import prompts, prompts_extended
from .doer_tools import adk_function_tools as _doer_tools
from .escalating_llm import EscalatingLlm
from .local_probe import maybe_substitute_primary


# Per-ticket override knob populated by ``adk_runner._process_one_ticket``
# before each ``build_pipeline`` call. None means "respect agent_config";
# a string means "force every archetype onto this provider for this run".
# Module-level because the ADK LlmAgent factory has no clean place to
# thread an option down through and the runner is single-shot anyway.
_FORCE_PROVIDER: str | None = None


def set_force_provider(name: str | None) -> None:
    """Pin every archetype to ``name`` (e.g. ``claude_local``) for the
    next pipeline build. Pass ``None`` to clear."""
    global _FORCE_PROVIDER
    _FORCE_PROVIDER = name


def _force_claude_local_cfg(role: str) -> dict:
    """Build a resolve_litellm-shaped dict that pins claude_local with
    the provider's default model — used when a ticket has attachments
    that only the subscription CLI can read."""
    from aiforge_core.config.agent_config import PROVIDERS as _PROV
    prov = _PROV["claude_local"]
    return {
        "model_id": prov["default_model"],
        "api_base": "claude:cli",
        "api_key": "",
        "_claude_cli": True,
    }


def build_litellm_model(role: str):
    """Return an :class:`EscalatingLlm` for the given role.

    Resolution order:

    1. Per-ticket force override (e.g. attachments → claude_local).
       Disables the cloud chain too — attachments only work through
       the subscription CLI, so falling back to a different provider
       would silently drop the file context.
    2. Operator profile via ``agent_config.resolve_litellm``.
    3. Pre-flight local-endpoint probe — if local mlx-lm is dead,
       swap to ``cloud_default_for_local`` (Ollama Cloud
       ``qwen3-coder-next`` by default) so the agent loop doesn't pay
       a failed-primary round-trip on every turn.

    EscalatingLlm wrapping always applies (primary → cloud chain →
    primary_retry); disable the chain with ``AIFORGE_ESCALATE_DISABLE=1``.
    """
    if _FORCE_PROVIDER == "claude_local":
        primary = _force_claude_local_cfg(role)
        return EscalatingLlm.build(role, primary, [])  # no chain — file
                                                       # context is local
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
    researcher = _agent("researcher", prompts_extended.RESEARCHER,
                        output_key="research_brief_md",
                        tools=_doer_tools())
    doer = _agent("doer", prompts.DOER, output_key="doer_outcome",
                  tools=_doer_tools())
    refiner = _agent("refiner", prompts_extended.REFINER,
                     output_key="refiner_changes")
    feedback = _agent("feedback", prompts.FEEDBACK, output_key="feedback_verdict")
    learner = _agent("learner", prompts.LEARNER, output_key="facts_json")

    doer_loop = LoopAgent(
        name="doer_refiner_feedback_loop",
        sub_agents=[doer, refiner, feedback],
        max_iterations=3,
    )
    return SequentialAgent(
        name="aiforge_v6_pipeline",
        sub_agents=[planner, verifier, researcher, doer_loop, learner],
    )


__all__ = ["build_pipeline", "build_litellm_model"]
