"""Code-context client — thin wrapper over AiForgeMemory.

Role-free per design. Any archetype calls `query()` directly.
"""
from __future__ import annotations


def query(text: str, *, repo: str, token_budget: int = 4000) -> str:
    """Return rendered Markdown ContextBundle for `text`."""
    from aiforge_memory.api.read import context_bundle_for
    return context_bundle_for(text, repo=repo, role="any",
                              token_budget=token_budget)
