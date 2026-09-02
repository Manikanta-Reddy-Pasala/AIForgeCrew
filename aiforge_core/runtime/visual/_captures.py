"""On-disk store for UI captures, so a screenshot outlives the tool call.

Kept OUT of the chat session's media dir on purpose: that folder backs the
"session files" the user attached, and dropping agent screenshots into it
would make them read as user uploads in the context block. Captures live under
the config root and are addressed by id, which is what ``ui_ask`` needs to
re-open an image the agent captured three steps ago.
"""
from __future__ import annotations

import os
import time
import uuid

_KEEP = 300          # captures retained; oldest pruned beyond this
_ID_LEN = 8


def captures_dir() -> str:
    root = os.environ.get("AIFORGE_CONFIG_DIR") or os.path.expanduser(
        "~/.aiforge")
    path = os.path.join(root, "captures")
    os.makedirs(path, exist_ok=True)
    return path


def _prune(dirpath: str) -> None:
    """Drop the oldest captures past :data:`_KEEP`. Best-effort."""
    try:
        entries = [os.path.join(dirpath, n) for n in os.listdir(dirpath)
                   if n.endswith(".png")]
        if len(entries) <= _KEEP:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        for old in entries[:len(entries) - _KEEP]:
            try:
                os.remove(old)
            except OSError:
                continue
    except OSError:
        pass


def save_capture(png: bytes, label: str = "ui") -> tuple[str, str] | tuple[None, None]:
    """``(capture_id, path)``, or ``(None, None)`` when the capture could not
    be stored.

    Storing is best-effort by design: an unwritable config dir (this project
    has met a root-owned ``~/.aiforge`` before) must cost the caller its
    capture id, not its whole result — the URL, the console errors and the
    audit are all still worth returning.
    """
    safe = "".join(c for c in (label or "ui") if c.isalnum() or c in "-_")[:24]
    capture_id = f"{safe or 'ui'}-{int(time.time())}-{uuid.uuid4().hex[:_ID_LEN]}"
    try:
        dirpath = captures_dir()
        path = os.path.join(dirpath, f"{capture_id}.png")
        with open(path, "wb") as fh:
            fh.write(png)
    except OSError:
        return None, None
    _prune(dirpath)
    return capture_id, path


def capture_path(capture_id: str) -> str | None:
    """Absolute path for ``capture_id``, or None. Rejects any id carrying a
    path separator — the id becomes a filename, and an agent-supplied
    ``../../etc/passwd`` must not be readable through it."""
    cid = (capture_id or "").strip()
    if not cid or os.path.basename(cid) != cid or cid.startswith("."):
        return None
    path = os.path.join(captures_dir(), f"{cid}.png")
    return path if os.path.isfile(path) else None


__all__ = ["captures_dir", "save_capture", "capture_path"]
