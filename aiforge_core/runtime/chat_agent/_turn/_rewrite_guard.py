"""A whole-file write that would throw away the turn's own earlier edits is
refused.

sqlglot v2a, 44 minutes in: 19/25 hidden tests passing, then ``file_write``
of all of ``parser.py`` from what the model remembered of the original. The
trigger parser it had added an hour earlier was gone ("must have been added
in an earlier session"), and the run ended at 3/25. The discard guard covers
``rm`` and ``git checkout``; this is the same loss through a write tool.

Refused, when ``file_write`` / ``file_create`` replaces a file a tool of THIS
turn already wrote (``st.file_hashes``) and the new content no longer defines
a function or class the file gained since its committed version. Reworded or
restructured lines are not a loss: counting dropped lines refused a shopkit
run's clean-ups five times and cost it the turn.
A file new this turn (not in HEAD) is not guarded: rewriting one's own draft
is normal. The model can make the change with file_patch / multi_edit, or send
the same write again with ``"drop_edits": true`` when dropping them is what it
means (its own key: file_write's ``force`` also skips the syntax check). Off
switch: ``AIFORGE_CHAT_REWRITE_GUARD=0``.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter

_WRITE_TOOLS = ("file_write", "file_create")
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_REWRITE_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _committed(path: str) -> "str | None":
    """The file at HEAD of its repo; '' when it is new there; None outside git."""
    d = os.path.dirname(path)
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode != 0:
            return None
        rel = os.path.relpath(path, top.stdout.strip())
        shown = subprocess.run(["git", "-C", top.stdout.strip(), "show", f"HEAD:{rel}"],
                               capture_output=True, text=True, timeout=5,
                               errors="replace")
        return shown.stdout if shown.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return None


def _lines(text: str) -> Counter:
    return Counter(ln.strip() for ln in (text or "").splitlines() if len(ln.strip()) > 3)


def lost_lines(before: str, now: str, new: str) -> list:
    """Lines ``now`` gained over ``before`` that ``new`` no longer has."""
    gained = _lines(now) - _lines(before)
    return list((gained - _lines(new)).elements())


def _defs(text: str) -> set:
    return {m.group(1) for m in map(_DEF.match, (text or "").splitlines()) if m}


def lost_defs(before: str, now: str, new: str) -> list:
    """Functions/classes ``now`` gained over ``before`` that ``new`` lacks."""
    return sorted((_defs(now) - _defs(before)) - _defs(new))


def refusal(st, name: str, args: dict, cwd) -> "dict | None":
    if name not in _WRITE_TOOLS or not _on() or not isinstance(args, dict):
        return None
    if args.get("drop_edits") is True or str(args.get("drop_edits")).lower() == "true":
        return None
    path = str(args.get("path") or "")
    content = args.get("content")
    if not path or not isinstance(content, str):
        return None
    path = os.path.expanduser(path)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or ".", path))
    edited = {os.path.realpath(p) for p in (getattr(st, "file_hashes", None) or {})}
    if full not in edited or not os.path.isfile(full):
        return None
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            now = fh.read()
    except OSError:
        return None
    before = _committed(full)
    if not before:
        return None                   # outside git, or new this turn: a draft
    gone = lost_defs(before, now, content)
    if not gone:
        return None
    return {"ok": False, "error": "rewrite_drops_your_edits",
            "lost_defs": gone[:10],
            "hint": (f"this whole-file write would drop {', '.join(gone[:6])} — "
                     f"added to {path} earlier in this turn. Keep them in the new "
                     "content, make the change with file_patch or multi_edit, or send "
                     "the same write with \"drop_edits\": true if dropping them is intended.")}
