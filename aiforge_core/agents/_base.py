"""Shared scaffolding for the per-archetype modules.

Each archetype module under ``aiforge_core.agents`` exports the same
five attributes (``ROLE``, ``PROMPT``, ``OUTPUT_KEY``, ``TOOLS_FACTORY``,
``build``). Centralising the construction here keeps each archetype
file at ~20 LOC of declarative metadata — the actual ADK plumbing lives
in one place so a wiring change touches exactly one file.

Usage from a per-archetype module::

    from . import _base
    ROLE = "doer"
    PROMPT = prompts.DOER
    OUTPUT_KEY = "doer_outcome"
    TOOLS_FACTORY = _doer_tools_factory   # callable returning list[FunctionTool]

    def build(model_factory):
        return _base.build_llm_agent(
            ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
        )
"""
from __future__ import annotations

from typing import Any, Callable

from .loader import AgentContract, load_agents


# Type alias for the per-role LLM factory the pipeline supplies.
# ``role -> EscalatingLlm-or-similar`` — the agent module never builds
# the model itself so provider routing stays in one module.
ModelFactory = Callable[[str], Any]


def contract_for(role: str) -> AgentContract:
    """Resolve the YAML contract for ``role``. Raises if the role is
    missing — that's a wiring bug worth surfacing loudly."""
    contracts = load_agents()
    if role not in contracts:
        raise KeyError(f"agents.yaml has no entry for role={role!r}")
    return contracts[role]


def build_llm_agent(role: str, instruction: str, output_key: str,
                    tools_factory: Callable[[], list] | None,
                    model_factory: ModelFactory):
    """Construct the ADK ``LlmAgent`` for one archetype.

    Args:
      role: must match an entry in ``agents.yaml``; used to pull
        ``timeout`` from the contract so per-role wall-clock caps stay
        co-located with the rest of the contract.
      instruction: the system prompt string consumed verbatim.
      output_key: ADK session-state key the agent writes its result to.
      tools_factory: zero-arg callable returning the FunctionTool list
        for this role. ``None`` means tool-less (judges, classifiers).
      model_factory: callable taking the role string and returning an
        ``EscalatingLlm`` (or any LiteLLM-compatible model object).
    """
    from google.adk.agents import LlmAgent

    c = contract_for(role)
    kwargs: dict[str, Any] = {
        "name": role,
        "model": model_factory(role),
        "instruction": instruction,
        "output_key": output_key,
        "timeout": c.contract.max_wall_s,
    }
    tools = tools_factory() if tools_factory else None
    if tools:
        kwargs["tools"] = tools
    # Per-archetype stage_start / stage_done events into ticket_events
    # so the UI's audit panel can show pipeline progress at the
    # archetype level (architect → planner → verifier → doer …) instead
    # of just status_change rows.
    from aiforge_core.runtime.observability import make_stage_callbacks
    before_cb, after_cb = make_stage_callbacks(role)
    if before_cb is not None:
        kwargs["before_agent_callback"] = before_cb
    if after_cb is not None:
        kwargs["after_agent_callback"] = after_cb
    return LlmAgent(**kwargs)


__all__ = ["ModelFactory", "contract_for", "build_llm_agent"]
