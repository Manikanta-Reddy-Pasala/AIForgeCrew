"""Doer archetype — the actual code-mutator.

Edits files inside the subticket's ``scope_allowlist_globs``, runs
compile + tests, halts on green or after the contract's failure
budget. The full FunctionTool surface lives in
``runtime.doer_tools`` so this module stays a thin metadata wrapper.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "doer"
PROMPT = prompts.DOER
OUTPUT_KEY = "doer_outcome"


def _tools_factory() -> list:
    """All Doer tools come from one place to keep the surface canonical."""
    from aiforge_core.runtime.doer_tools import adk_function_tools
    return adk_function_tools()


TOOLS_FACTORY = _tools_factory


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
