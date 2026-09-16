"""Stored copies of chat events keep tool results short.

A run that goes on for hours emits thousands of tool events, and a file read
can return 80 KB. The live stream gets every event whole; the re-attach buffer
(``chat_runs``) and the persisted turn steps keep a copy whose long result
strings are cut, so memory and the saved message stay bounded. Everything a
reader relies on (``ok``, error text, paths, exit codes) is short and survives.
"""
from __future__ import annotations

import json
import os

_MARK = "\n…[{n} more characters not kept in the saved step]"


def _cap() -> int:
    try:
        return max(500, int(os.environ.get("AIFORGE_CHAT_STORED_RESULT_CHARS", "8000")))
    except ValueError:
        return 8000


#: Characters of an approval card kept per value.
_APPROVAL_CAP = 1_000_000
#: List items kept in a stored copy (a grep can return thousands).
_MAX_ITEMS = 200


def _cut(value, cap: int, depth: int):
    if isinstance(value, str):
        if len(value) <= cap:
            return value
        return value[:cap] + _MARK.format(n=len(value) - cap)
    if not isinstance(value, (dict, list)):
        return value
    if depth <= 0:
        # Too deep to walk: keep it only if it is small once written out.
        text = json.dumps(value, default=str, ensure_ascii=False)
        return value if len(text) <= cap else _cut(text, cap, 0)
    if isinstance(value, dict):
        return {k: _cut(v, cap, depth - 1) for k, v in value.items()}
    kept = [_cut(v, cap, depth - 1) for v in value[:_MAX_ITEMS]]
    if len(value) > _MAX_ITEMS:
        kept.append(f"…[{len(value) - _MAX_ITEMS} more items not kept in the saved step]")
    return kept


def slim_event(event: dict) -> dict:
    """The event to store: a copy of a tool event with long argument and
    result values cut; any other event is returned as is."""
    kind = event.get("type")
    if kind == "approval":
        # Shown again on re-attach: the user must see the change they are
        # approving, so only a pathological card is cut.
        keys, cap = ("args", "preview"), _APPROVAL_CAP
    elif kind == "tool":
        keys, cap = ("args", "result"), _cap()
    else:
        return event
    present = [k for k in keys if k in event]
    if not present:
        return event
    return {**event, **{k: _cut(event[k], cap, 3) for k in present}}
