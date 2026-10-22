"""FINAL scope net: files the model changed under work it added itself.

A board item the model put on the board whose title shares no content word
with the request, a steer or a harness item is tagged ``self_added``
(``_turn/_tasks.apply_progress``); when an item closes,
``_turn/_items.review_progress`` records the files edited in its window as
``files``. At FINAL the changed files that only self-added items account for
are named in the answer (``block``) and sent to the UI (``event``) so the
user can revert them. No model call; nothing is reverted without the click.
With item logging inactive (a board under 3 items) no item records files and
the net reports nothing."""
from __future__ import annotations

import os
import posixpath

#: Paths named in the answer before "(+N more)".
_SHOWN = 12


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_SCOPE_NET", "1").strip().lower() not in ("0", "false", "no", "off")


def prefix_of(cwd) -> str:
    """The work folder's path inside its repository (``git rev-parse
    --show-prefix``: "" at the top level, "sub/" below it)."""
    if not cwd:
        return ""
    from .turn_facts import _git
    return (_git(cwd, "rev-parse", "--show-prefix") or "").strip()


def _top(prefix: str, path: str) -> str:
    """``path`` (relative to the work folder) relative to the repository."""
    return posixpath.normpath(posixpath.join(prefix or "", path.replace(os.sep, "/")))


def unrequested(st, changed, prefix: str = "") -> list[str]:
    """Changed files whose only item evidence is a self-added board item,
    as the items name them (relative to the work folder). ``changed`` is
    relative to the repository, item files to the work folder; ``prefix``
    (:func:`prefix_of`) puts them in one space. A file an item of the request
    also touched belongs to the request; with no request item that recorded
    files there is nothing to compare against, and nothing is flagged."""
    board = getattr(st, "board", None) or {}
    own: dict = {}
    asked: set = set()
    for it in board.values():
        files = it.get("files") or []
        if it.get("self_added"):
            own.update((_top(prefix, f), f) for f in files)
        else:
            asked.update(_top(prefix, f) for f in files)
    if not asked:
        return []
    hit = (set(own) - asked) & {_top("", p) for p in (changed or ())}
    return sorted(own[p] for p in hit)


def block(paths) -> str:
    """The line the answer ends with, or ""."""
    paths = list(paths or ())
    if not paths:
        return ""
    shown = ", ".join(f"`{p}`" for p in paths[:_SHOWN])
    more = len(paths) - _SHOWN
    return "\n\n⚠ Unrequested changes: " + shown + (f" (+{more} more)" if more > 0 else "")


def event(st, paths) -> dict:
    """The UI event behind the revert button."""
    return {"type": "scope_net", "paths": list(paths), "head0": getattr(st, "head0", None)}


__all__ = ["block", "enabled", "event", "prefix_of", "unrequested"]
