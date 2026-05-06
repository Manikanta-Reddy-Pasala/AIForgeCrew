"""Agent contract loader.

Re-exports the YAML-driven contract API:
    from aiforge_core.agents import AgentContract, load_agents
"""
from __future__ import annotations

from .loader import (  # noqa: F401
    AgentContract,
    AgentSpecError,
    load_agents,
    tools_schema_for_role,
    validate_contracts,
)
