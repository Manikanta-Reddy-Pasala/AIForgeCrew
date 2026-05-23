"""Live-verifier archetype — boots the candidate fix end-to-end.

Runs after Validator approves the diff, before ``git_pr`` opens the
PR. Reads a per-project recipe from
``aiforge_core/recipes/live_verify/<project>.md`` (fallback
``_default.md``) and follows it with bash + file_read tools.

The pipeline builder substitutes ``{recipe_md}`` into the prompt at
build time — same trick as the prompt-frame for other archetypes —
so the agent receives the recipe inline rather than going through a
tool call to fetch it.
"""
from __future__ import annotations

import os
from pathlib import Path

from aiforge_core.runtime import prompts

from . import _base

ROLE = "live_verifier"
OUTPUT_KEY = "live_verifier_verdict"

_RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes" / "live_verify"


def _tools_factory() -> list:
    """Bash + file_read are enough for every recipe. Reuses the Doer's
    tool surface so behaviour (sandboxing, AIFORGE_REPO_ROOT scope) is
    identical to what the Doer just used."""
    from aiforge_core.runtime.doer_tools import adk_function_tools
    all_tools = adk_function_tools()
    keep = {"bash", "file_read", "file_grep"}
    filtered = [
        t for t in all_tools
        if getattr(t, "name", "") in keep or getattr(t, "__name__", "") in keep
    ]
    return filtered or all_tools


TOOLS_FACTORY = _tools_factory


def load_recipe(project: str | None) -> str:
    """Return the recipe markdown for ``project``. Falls back to
    ``_default.md`` for unknown projects; returns a short stub when
    even the default is missing (test environments)."""
    candidates: list[Path] = []
    if project:
        candidates.append(_RECIPES_DIR / f"{project}.md")
    candidates.append(_RECIPES_DIR / "_default.md")
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError:
                continue
    return (
        "# Live verify — fallback\n\n"
        "No recipe found. Run the project's standard test command "
        "(`./mvnw test`, `npm test`, `pytest`) and emit "
        "`{\"ok\": exit==0, \"rationale\": \"...\"}`.\n"
    )


def _prompt_for_project(project: str | None) -> str:
    return prompts.LIVE_VERIFIER.replace("{recipe_md}", load_recipe(project))


def build(model_factory: _base.ModelFactory, project: str | None = None):
    """Build the live_verifier agent with the project-specific recipe
    baked into the prompt."""
    prompt = _prompt_for_project(project or os.environ.get(
        "AIFORGE_TICKET_PROJECT", "",
    ))
    return _base.build_llm_agent(
        ROLE, prompt, OUTPUT_KEY, TOOLS_FACTORY, model_factory,
    )


__all__ = [
    "ROLE", "OUTPUT_KEY", "TOOLS_FACTORY",
    "load_recipe", "build",
]
