"""Archetype implementations. Importing this package registers them all."""
from __future__ import annotations

# Side-effect imports — each module @register('s its class
from . import (  # noqa: F401
    architect,
    doer,
    grounder,
    learner,
    planner,
    tester,
    understander,
    validator,
    verifier,
)

# Re-export loader contracts so callers can do:
#   from aiforge_core.agents import AgentContract, load_agents
from .loader import (  # noqa: F401
    AgentContract,
    AgentSpecError,
    load_agents,
    tools_schema_for_role,
    validate_contracts,
)
