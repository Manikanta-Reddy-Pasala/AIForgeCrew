"""FINAL scope net: files the model changed under work it added itself.

A board item the model put on the board with no matching user cue is tagged
``self_added`` (``_turn/_tasks.apply_progress``); when such an item closes,
``_turn/_items.review_progress`` records the files edited in its window as
``files``. At FINAL the files that only such items account for are named in
the answer (``block``) and sent to the UI (``event``) so the user can revert
them. No model call; nothing is reverted without the user's click."""
from __future__ import annotations

import os


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SCOPE_NET", "1").strip().lower() not in ("0", "false", "no", "off")


def unrequested(st, changed) -> list[str]:
    """Changed files whose only item evidence is a self-added board item.
    A file an item of the request also touched belongs to the request."""
    board = getattr(st, "board", None) or {}
    own, asked = set(), set()
    for it in board.values():
        files = set(it.get("files") or [])
        (own if it.get("self_added") else asked).update(files)
    return sorted((own - asked) & set(changed or ()))


def block(paths) -> str:
    """The line the answer ends with, or ""."""
    if not paths:
        return ""
    return "\n\n⚠ Unrequested changes: " + ", ".join(f"`{p}`" for p in paths)


def event(st, paths) -> dict:
    """The UI event behind the revert button."""
    return {"type": "scope_net", "paths": list(paths), "head0": getattr(st, "head0", None)}


__all__ = ["block", "enabled", "event", "unrequested"]
