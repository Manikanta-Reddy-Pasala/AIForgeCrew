"""Project conventions loader.

Reads ``.aiforge/CONVENTIONS.md`` from the worktree (Aider's
CONVENTIONS.md analogue, our naming) and returns its text. The
doer's ``_build_user_input`` prepends it as a ``## Project
conventions`` section so per-repo idioms (Lombok @Slf4j,
@AllArgsConstructor, feature-module layout, naming, banned
patterns) are always in the prompt.

Capped at 8 KB to avoid blowing the context budget; the rest is
truncated with a banner pointing the model at the file path.
"""
from __future__ import annotations

import os

_CAP = 8 * 1024


def load(worktree: str) -> str:
    """Return the conventions text, or empty string when absent."""
    path = os.path.join(worktree, ".aiforge", "CONVENTIONS.md")
    if not os.path.isfile(path):
        return ""
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > _CAP:
        text = text[:_CAP] + (
            f"\n\n[truncated; full file at .aiforge/CONVENTIONS.md "
            f"({os.path.getsize(path)} bytes)]"
        )
    return text


def section_for_prompt(worktree: str) -> str:
    """Return a ``## Project conventions\\n<text>\\n\\n`` block, or empty."""
    body = load(worktree)
    if not body:
        return ""
    return f"## Project conventions (from .aiforge/CONVENTIONS.md)\n{body}\n\n"
