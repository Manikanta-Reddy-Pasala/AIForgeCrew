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

    We reuse ``runtime.doer_tools.adk_function_tools`` for the full
    catalogue — the prompt tells the model not to call write tools, and
    the YAML ``forbidden`` list backstops that contract at the harness
    layer. KISS: one tool factory, declarative scope, no duplicate
    schemas.
    """
    from aiforge_core.runtime.doer_tools import adk_function_tools
    return adk_function_tools()


TOOLS_FACTORY = _tools_factory


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
