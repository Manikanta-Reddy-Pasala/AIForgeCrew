"""Context that leaves the prompt is saved, not thrown away.

A condense used to delete the middle of a long chat and leave a one-line note
("Re-read a file or ask the user"). The full text of those messages is saved
here under a content id instead, and the note names the id: the model restores
what it needs with ``memory_lookup {"id": ...}``, a page at a time. Nothing is
lost, and nothing is paid for until the model asks.

Content-addressed (the same text is one file), best-effort (a failed save falls
back to the old note), and old files expire.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

PREFIX = "off:"
_MAX_CHARS = 400_000          # one saved block
_PAGE = 12_000                # one restore returns at most this much
_KEEP_DAYS = 14


def _dir() -> Path:
    from aiforge_core.config.paths import config_dir
    d = Path(os.path.expanduser(str(config_dir()))) / "context_offload"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(oid: str) -> "Path | None":
    key = oid[len(PREFIX):] if oid.startswith(PREFIX) else ""
    if not key or not key.isalnum():
        return None
    return _dir() / f"{key}.txt"


def _expire(d: Path) -> None:
    cutoff = time.time() - _KEEP_DAYS * 86400
    for f in d.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def render(messages: list) -> str:
    """The text of ``messages``, one block per message, in order."""
    from aiforge_core.runtime.chat_agent._context._compaction import _text_of
    parts = []
    for m in messages or []:
        if isinstance(m, dict):
            parts.append(f"[{m.get('role', '?')}]\n{_text_of(m)}")
    return "\n\n".join(parts)


def save(text: str) -> "str | None":
    """Save ``text`` and return its id, or None when it could not be saved."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        text = text[:_MAX_CHARS]
        oid = PREFIX + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        d = _dir()
        p = d / f"{oid[len(PREFIX):]}.txt"
        if not p.exists():
            p.write_text(text, encoding="utf-8")
            _expire(d)
        return oid
    except Exception:  # noqa: BLE001 — never breaks a turn
        return None


def load(oid: str, offset: int = 0, limit: int = _PAGE) -> "dict | None":
    """One page of a saved block: ``{id, text, offset, total, next_offset}``."""
    try:
        p = _path(oid)
        if p is None or not p.exists():
            return None
        body = p.read_text(encoding="utf-8")
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or _PAGE), _PAGE))
        end = offset + limit
        return {"id": oid, "text": body[offset:end], "offset": offset,
                "total": len(body), "next_offset": end if end < len(body) else None}
    except Exception:  # noqa: BLE001
        return None


__all__ = ["PREFIX", "render", "save", "load"]
