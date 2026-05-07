"""Architect archetype — placeholder.

The Architect is an EXTERNAL Claude Code session driven by the human
operator. There's no ADK ``LlmAgent`` to construct — this module
exists so the per-archetype module set is complete and tooling that
imports ``from aiforge_core.agents import architect`` succeeds.

If you need the Architect's contract (tool allowlist, max_turns),
load it via :func:`aiforge_core.agents.loader.load_agents`.
"""
from __future__ import annotations

from . import _base

ROLE = "architect"
PROMPT = ""        # external Claude Code provides its own system prompt
OUTPUT_KEY = ""    # nothing in ADK session-state — Architect writes tickets directly to Postgres
TOOLS_FACTORY = None


def build(model_factory):  # noqa: ARG001
    """Architect is external; no ADK agent to build.

    Returning ``None`` lets pipeline-builder code branch on presence
    rather than special-casing the role name.
    """
    return None


def contract():
    """Convenience wrapper around the YAML contract for this role."""
    return _base.contract_for(ROLE)


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY",
           "build", "contract"]
