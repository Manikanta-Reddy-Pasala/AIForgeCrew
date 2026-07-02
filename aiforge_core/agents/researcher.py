"""Researcher archetype — read-only context gatherer.

Sits between Verifier and the Doer loop. For each child subticket it
calls ``graphify_lookup`` / ``memory_lookup`` / ``file_read`` /
``list_dir`` to assemble a research brief the Doer consumes verbatim.
Stops as soon as each subticket has at least one relevant_files entry
— the prompt explicitly tells the model not to over-research.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts_extended

from . import _base

ROLE = "researcher"
PROMPT = prompts_extended.RESEARCHER
OUTPUT_KEY = "research_brief_md"


def _tools_factory() -> list:
    """Researcher uses the Doer's read tools but writes nothing.

    Pass ROLE so the factory enforces researcher's agents.yaml allowlist
    (graphify_lookup / memory_lookup / editor / grep_repo / grep / file_read /
    list_dir / repo_map) — a SUPERSET of the read tools the prompt calls.
    Write/exec tools (file_write, file_patch, bash, run_shell, git_commit)
    are stripped structurally, so the read-only contract no longer relies on
    the model honouring the prompt. KISS: one tool factory, declarative scope.
    """
    from aiforge_core.runtime.doer_tools import adk_function_tools
    return adk_function_tools(role=ROLE)


TOOLS_FACTORY = _tools_factory


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
