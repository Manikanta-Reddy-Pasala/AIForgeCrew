"""Archetype implementations. Importing this package registers them all."""
from __future__ import annotations

# Side-effect imports — each module @register('s its class.
# v5 production pipeline: architect, planner, verifier, doer, learner.
# Feedback runs as a model judge (no archetype class) inside the doer
# loop, see runtime.adk_runner.
from . import (  # noqa: F401
    architect,
    doer,
    learner,
    planner,
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
