"""Self-editing memory block tool (frontier gap #8, Letta-style).

The agent owned a persistent, named scratchpad per repo that it maintained
itself. This was backed by the optional AiForgeMemory graph blocks store,
which has been removed (SQLite-only build), so the tool is a soft no-op —
the durable working-notes role is covered by the md/OKR memory instead.
"""
from __future__ import annotations

import os


def _repo() -> str:
    return os.environ.get("AIFORGE_AFM_REPO", "").strip() or ""


def memory_block(action: str = "read", content: str = "",
                 label: str = "working_notes") -> dict:
    """Read or self-edit your persistent memory block for this repo.

    The graph-backed block store was removed, so this soft-fails; callers
    never raise. Returns ``{ok: False, error}``.
    """
    repo = _repo()
    if not repo:
        return {"ok": False, "error": "no repo scope (AIFORGE_AFM_REPO unset)"}
    return {"ok": False,
            "error": "memory block backend removed (SQLite-only build)"}


__all__ = ["memory_block"]
