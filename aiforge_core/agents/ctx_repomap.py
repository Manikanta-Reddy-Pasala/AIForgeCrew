"""ctx_repomap archetype — repo-map / code-search context gatherer.

One branch of the ParallelAgent context stage. Read-only; writes
``repo_brief_md``. Reuses the Doer read-tool catalogue; prompt + YAML
``forbidden`` list keep it from writing.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts_extended

from . import _base

ROLE = "ctx_repomap"
PROMPT = prompts_extended.CTX_REPOMAP
OUTPUT_KEY = "repo_brief_md"


def _tools_factory() -> list:
    from aiforge_core.runtime.doer_tools import adk_function_tools
    return adk_function_tools()


TOOLS_FACTORY = _tools_factory


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
