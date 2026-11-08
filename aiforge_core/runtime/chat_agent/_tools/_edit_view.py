"""The lines an edit changed, as they now read, returned with the edit.

Bench (attrs-inspect, 11-08): an edit came back as ``{"ok": true}`` only, so
after 25 of its 65 patches the model read the file again just to see the
result and the shifted line numbers — a model call each. With the changed
lines (and a few around them) in the result, that read is not needed.

Kept small so the history does not grow faster than the reads it replaces:
a few lines of context, a line and a character cap. ``AIFORGE_CHAT_EDIT_VIEW=0``
turns it off.
"""
from __future__ import annotations

import io
import os

_CTX = 3
_MAX_LINES = 20
_MAX_CHARS = 800


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_EDIT_VIEW", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def view(before: str, after: str) -> "dict | None":
    """``{"start", "end", "text"}``: the changed lines of ``after`` with a few
    lines around them (1-based line numbers), or None (no change, or off)."""
    if not _on() or before == after:
        return None
    # split on "\n" only, as read_lines counts (splitlines also breaks on
    # form feeds and other separators, which shifts the line numbers)
    old = io.StringIO(before, newline="\n").readlines()
    new = io.StringIO(after, newline="\n").readlines()
    p = 0
    while p < len(old) and p < len(new) and old[p] == new[p]:
        p += 1
    q = 0
    while q < len(old) - p and q < len(new) - p and old[-1 - q] == new[-1 - q]:
        q += 1
    lo = max(0, p - _CTX)
    hi = min(len(new), len(new) - q + _CTX)
    if hi <= lo:                                  # only lines removed
        hi = min(len(new), lo + 2 * _CTX)
    cut = hi - lo > _MAX_LINES
    hi = min(hi, lo + _MAX_LINES)
    text = "".join(new[lo:hi])
    if len(text) > _MAX_CHARS:
        text, cut = text[:_MAX_CHARS], True
    out = {"start": lo + 1, "end": hi, "text": text}
    if cut:
        out["note"] = "cut: read_lines for the rest of the change"
    return out
