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

from aiforge_core.agents import (
    doer as _doer_mod,
    feedback as _feedback_mod,
    learner as _learner_mod,
    planner as _planner_mod,
    refiner as _refiner_mod,
    researcher as _researcher_mod,
    verifier as _verifier_mod,
)
from aiforge_core.config import agent_config as _acfg

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


def build_pipeline(*, skip_researcher: bool = False):
    """Construct the SequentialAgent. Returns the root agent ready for
    ``Runner(agent=..., session_service=...)``.

    Each archetype is built by its own module under
    ``aiforge_core.agents.*`` — the call site below is the ONLY place
    that knows the order in which they run. Adding a new role = drop a
    module and slot it into the right list here; the per-archetype
    files stay declarative.

    Args:
      skip_researcher: when True, omit Researcher from the pipeline.
        Caller (typically :mod:`adk_runner`) decides via
        :func:`researcher_routing.should_skip_researcher`. Saves
        5+ LM calls on greenfield tickets where the Researcher would
        find nothing relevant anyway.
    """
    from google.adk.agents import LoopAgent, SequentialAgent
    from .loop_budget import build_loop_budget_callbacks

    planner = _planner_mod.build(build_litellm_model)
    verifier = _verifier_mod.build(build_litellm_model)
    researcher = _researcher_mod.build(build_litellm_model)
    doer = _doer_mod.build(build_litellm_model)
    refiner = _refiner_mod.build(build_litellm_model)
    feedback = _feedback_mod.build(build_litellm_model)
    learner = _learner_mod.build(build_litellm_model)
    # Persist Learner-emitted facts into Neo4j (Observation_v2 +
    # Decision_v2). Without this, state['facts_json'] dies with the
    # session — the memory layer stayed near-empty across 8 days of
    # tickets. The callback reads ticket/repo info already populated
    # by adk_runner before the pipeline starts.
    from .learner_persist import make_learner_after_callback
    _existing_learner_after = learner.after_agent_callback
    _learner_persist_cb = make_learner_after_callback()
    _merged_learner_after: list = []
    if _existing_learner_after is not None:
        if isinstance(_existing_learner_after, list):
            _merged_learner_after.extend(_existing_learner_after)
        else:
            _merged_learner_after.append(_existing_learner_after)
    _merged_learner_after.append(_learner_persist_cb)
    learner.after_agent_callback = _merged_learner_after

    # Doer / Refiner / Feedback live inside the loop so a Feedback
    # rejection rewinds the polish-then-judge cycle, not just the Doer.
    # ``before_agent_callback`` runs once per LoopAgent invocation, but
    # we attach the LOC-plateau watcher to the Refiner — it sees every
    # loop turn AFTER the Doer has emitted its file_diffs payload,
    # which is the cheapest place to compute the LOC delta.
    plateau_before, plateau_after = build_loop_budget_callbacks()
    if plateau_before is not None:
        # Attach to the Refiner's after-callback so we see the loop
        # iteration's LOC outcome AFTER the Doer reported file_diffs
        # but BEFORE Feedback wastes a turn judging a stuck loop.
        existing_after = refiner.after_agent_callback
        merged_after: list = []
        if existing_after is not None:
            if isinstance(existing_after, list):
                merged_after.extend(existing_after)
            else:
                merged_after.append(existing_after)
        merged_after.append(plateau_after)
        refiner.after_agent_callback = merged_after

    sub_agents: list = [planner, verifier]
    if not skip_researcher:
        sub_agents.append(researcher)
    doer_loop = LoopAgent(
        name="doer_refiner_feedback_loop",
        sub_agents=[doer, refiner, feedback],
        max_iterations=3,
        # ``plateau_before`` aborts the LoopAgent at the start of the
        # next iteration when state['loop_budget_kill'] is set.
        before_agent_callback=plateau_before,
    )
    sub_agents.append(doer_loop)
    sub_agents.append(learner)
    return SequentialAgent(
        name="aiforge_v6_pipeline",
        sub_agents=sub_agents,
    )


__all__ = ["build_pipeline", "build_litellm_model"]
