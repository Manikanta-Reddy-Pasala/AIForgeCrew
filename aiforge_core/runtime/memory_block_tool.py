"""Self-editing memory block tool (frontier gap #8, Letta-style).

The agent owns a persistent, named scratchpad per repo that it maintains
itself — read it, rewrite it, append to it. Unlike ``memory_lookup``
(retrieve mined facts) this is a single living document the agent keeps:
working notes, a running TODO, accumulated conventions. Persists across
turns and tickets. Backed by AiForgeMemory ``features.memory.blocks``
(``:MemoryBlock`` singleton per repo+label); neo4j-only, soft-fail.
"""
from __future__ import annotations

import os


def _repo() -> str:
    return os.environ.get("AIFORGE_AFM_REPO", "").strip() or ""


def memory_block(action: str = "read", content: str = "",
                 label: str = "working_notes") -> dict:
    """Read or self-edit your persistent memory block for this repo.

    Args:
      action: ``read`` (return the block), ``write`` (overwrite it with
        ``content``), or ``append`` (add ``content`` as a new line).
      content: new text for write/append (ignored for read).
      label: which block — default ``working_notes``. Use a stable label
        to keep separate blocks (e.g. ``conventions``, ``todo``).

    Returns ``{ok, label, content?|chars?}`` or ``{ok: False, error}``.
    The block survives across turns/tickets — use it to remember durable
    working context you maintain yourself, distinct from mined facts."""
    repo = _repo()
    if not repo:
        return {"ok": False, "error": "no repo scope (AIFORGE_AFM_REPO unset)"}
    try:
        from .learner_persist import _open_driver
        driver = _open_driver()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"memory backend missing: {exc}"}
    if driver is None:
        return {"ok": False, "error": "no neo4j driver (embedded mode)"}
    try:
        from aiforge_memory.features.memory import blocks
        act = (action or "read").lower()
        if act == "read":
            return {"ok": True, "label": label,
                    "content": blocks.get_block(driver, repo=repo, label=label)}
        if act == "write":
            r = blocks.set_block(driver, repo=repo, label=label,
                                 content=content)
            return {"ok": True, "label": label, "chars": r["chars"]}
        if act == "append":
            r = blocks.append_block(driver, repo=repo, label=label,
                                    line=content)
            return {"ok": True, "label": label, "chars": r["chars"]}
        return {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:  # noqa: BLE001 — never raise into the agent loop
        return {"ok": False, "error": f"memory_block failed: {exc}"}
    finally:
        try:
            driver.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["memory_block"]
