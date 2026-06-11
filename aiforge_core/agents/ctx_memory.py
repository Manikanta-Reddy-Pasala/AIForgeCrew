"""ctx_memory archetype — memory-recall context gatherer.

One branch of the ParallelAgent context stage (see
:func:`runtime.pipeline.build_pipeline`). Read-only; writes
``memory_brief_md``. Reuses the Doer read-tool catalogue — the prompt
+ YAML ``forbidden`` list keep it from writing.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts_extended

from . import _base

ROLE = "ctx_memory"
PROMPT = prompts_extended.CTX_MEMORY
OUTPUT_KEY = "memory_brief_md"


def _tools_factory() -> list:
    from aiforge_core.runtime.doer_tools import adk_function_tools
    return adk_function_tools()


TOOLS_FACTORY = _tools_factory


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
