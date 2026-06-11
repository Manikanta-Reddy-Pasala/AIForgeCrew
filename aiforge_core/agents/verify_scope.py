"""verify_scope archetype — scope axis of the parallel Verifier.

One branch of the ParallelAgent verifier stage. Tool-less single-turn
JSON critic; writes ``verify_scope``. Merged into ``verifier_verdict``
by the verifier-merge callback.
"""
from __future__ import annotations

from aiforge_core.runtime import prompts

from . import _base

ROLE = "verify_scope"
PROMPT = prompts.VERIFY_SCOPE
OUTPUT_KEY = "verify_scope"
TOOLS_FACTORY = None


def build(model_factory: _base.ModelFactory):
    return _base.build_llm_agent(
        ROLE, PROMPT, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = ["ROLE", "PROMPT", "OUTPUT_KEY", "TOOLS_FACTORY", "build"]
