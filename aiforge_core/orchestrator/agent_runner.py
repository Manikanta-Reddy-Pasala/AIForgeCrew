"""Agent runner — wraps ADK Runner around a registered archetype.

Wires:
    - registry.build(name) → BaseArchetype instance
    - tool_registry → ADK tool list
    - 7 callbacks chained in fixed order (compactor / auditor /
      breakers / stuck / failure_taxonomy / learner)
    - prompt_registry → system prompt for the version pinned

Public:
    runner.run_archetype(name, ctx) -> ArtifactDict

ADK is imported lazily so unit tests can run without it.
"""
from __future__ import annotations

from typing import Any

from aiforge_core.agents import registry
from aiforge_core.orchestrator import circuit_breakers as cb_mod


def run_archetype(name: str, *, ctx: dict[str, Any] | None = None,
                  breakers: cb_mod.CircuitBreakers | None = None,
                  ) -> dict[str, Any]:
    """Build + run an archetype. Tests can call this without ADK."""
    ctx = ctx or {}
    breakers = breakers or cb_mod.CircuitBreakers()
    breakers.begin_agent(name)

    agent = registry.build(name, **ctx.get("ctor", {}))
    try:
        return agent.run(ctx=ctx)
    finally:
        breakers.check_agent(name)
