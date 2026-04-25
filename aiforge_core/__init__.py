"""AIForgeCrew runtime package (v5).

Public API surface re-exported here intentionally narrow — the canonical
agent contracts loaded from ``agents.yaml``. Subpackages
(``doer``, ``planner``, ``index``, ``memory``, ``eval``, ``runtime``) are
imported directly by callers; legacy v4 code lives in ``aiforge_core.legacy``
pending Phase 11 removal.
"""
from __future__ import annotations

from .agents import AgentContract, load_agents

__all__ = ["AgentContract", "load_agents"]
