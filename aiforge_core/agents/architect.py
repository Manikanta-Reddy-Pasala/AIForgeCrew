"""Architect archetype — external Claude Code, structural-plan emitter.

The Architect is an EXTERNAL Claude Code session driven by the human
operator. There's no ADK ``LlmAgent`` to construct — this module
exists so the per-archetype module set is complete and tooling that
imports ``from aiforge_core.agents import architect`` succeeds.

The ``PROMPT`` constant exposes the structural-plan contract the
external Architect must follow (file tree + symbol owners + per-file
import allowlist). The runtime surfaces the Architect's output into
ADK session state under ``structural_plan`` so the Doer can look up
the canonical owner of any symbol it imports — fixing the ONE-117
"guessed wrong location for StockMovement" class of bug.

If you need the Architect's contract (tool allowlist, max_turns),
load it via :func:`aiforge_core.agents.loader.load_agents`.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "architect"
PROMPT = prompts.ARCHITECT  # structural-plan contract — see prompts/architect.py
OUTPUT_KEY = "structural_plan"  # session-state key the Doer reads from
TOOLS_FACTORY = None


def build(model_factory):  # noqa: ARG001
    """Architect is external; no ADK agent to build.

    Returning ``None`` lets pipeline-builder code branch on presence
    rather than special-casing the role name. The PROMPT constant is
    consumed by the EXTERNAL Claude Code session that drives ticket
    creation — it's documented here so the contract review surface
    lives next to every other archetype.
    """
    return None


def contract():
    """Convenience wrapper around the YAML contract for this role."""
    return _base.contract_for(ROLE)


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY",
           "build", "contract"]
