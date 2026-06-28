"""Tiny mtime-keyed JSON read cache for the hot config files.

``agent_config.json`` / ``model_overrides.json`` / ``runtime_settings.json``
were read + JSON-parsed on EVERY access. With the LLM retry/escalation
layers (primary → fallback → escalate) that amplified to 6+ parses of the
same unchanged file per ``client.complete()`` call. This caches the parsed
result keyed by (path, mtime): a UI/API write rewrites the file → mtime
changes → cache auto-invalidates, so it's always fresh without polling.

Returns a deep copy on every call, so read-modify-write callers (set_role,
persist, runtime_settings.set) can mutate freely without corrupting the
cache. The saved cost is the JSON PARSE (the expensive part); a deep copy
of a small config dict is far cheaper than re-decoding the file.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

# path(str) -> (mtime, parsed)
_CACHE: dict[str, tuple[float, Any]] = {}


def read_json(path: str | Path) -> Any | None:
    """Return parsed JSON for *path*, cached by mtime. ``None`` when the
    file is missing or unparseable (callers fall back to defaults)."""
    p = str(path)
    try:
        mt = os.stat(p).st_mtime
    except OSError:
        _CACHE.pop(p, None)
        return None
    hit = _CACHE.get(p)
    if hit is not None and hit[0] == mt:
        return copy.deepcopy(hit[1])
    try:
        data = json.loads(Path(p).read_text())
    except Exception:  # noqa: BLE001 — unreadable/corrupt → treat as absent
        return None
    _CACHE[p] = (mt, data)
    return copy.deepcopy(data)


def clear() -> None:
    """Test-only / reset hook — drop all cached parses."""
    _CACHE.clear()
