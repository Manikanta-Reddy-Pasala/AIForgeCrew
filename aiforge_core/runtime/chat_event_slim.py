"""Stored copies of chat events keep tool results short.

A run that goes on for hours emits thousands of tool events, and a file read
can return 80 KB. The live stream gets every event whole; the re-attach buffer
(``chat_runs``) and the persisted turn steps keep a copy whose long result
strings are cut, so memory and the saved message stay bounded. Everything a
reader relies on (``ok``, error text, paths, exit codes) is short and survives.
"""
from __future__ import annotations

import os

_MARK = "\n…[{n} more characters not kept in the saved step]"


def _cap() -> int:
    try:
        return max(500, int(os.environ.get("AIFORGE_CHAT_STORED_RESULT_CHARS", "8000")))
    except ValueError:
        return 8000


def _cut(value, cap: int, depth: int):
    if isinstance(value, str):
        if len(value) <= cap:
            return value
        return value[:cap] + _MARK.format(n=len(value) - cap)
    if depth <= 0:
        return value
    if isinstance(value, dict):
        return {k: _cut(v, cap, depth - 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, cap, depth - 1) for v in value]
    return value


def slim_event(event: dict) -> dict:
    """The event to store: a copy of a tool event with long result strings
    cut; any other event is returned as is."""
    if event.get("type") != "tool" or "result" not in event:
        return event
    return {**event, "result": _cut(event["result"], _cap(), 3)}
