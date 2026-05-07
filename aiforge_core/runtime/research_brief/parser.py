"""Researcher-output parser.

Owns nothing but parsing — turning raw model text into a list of brief
dicts. Rendering and persistence live in their own modules so a tweak
to the salvage path doesn't risk the markdown layout.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Markdown code-fence wrapper — local models often wrap JSON in ```json...```
# even when the prompt asks for raw JSON. Strip it before parsing.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Return the inside of a ```json ... ``` block, or ``text`` unchanged."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text


def _find_balanced_array(text: str) -> str | None:
    """Return the first top-level ``[...]`` substring, or None.

    Used as a salvage path when the model emitted prose around a valid
    JSON array (e.g. "Sure, here you go: [...]. Let me know."). We bail
    on the first nesting mismatch — no recursive recovery — to keep the
    parser simple and predictable.
    """
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _list_of_dicts(v: Any) -> list[dict]:
    return [x for x in (v or []) if isinstance(x, dict)]


def _list_of_str(v: Any) -> list[str]:
    return [str(x) for x in (v or []) if isinstance(x, (str, int, float))]


def _coerce_brief(entry: dict) -> dict:
    """Project a raw brief dict onto the canonical 5-field shape.

    Missing / wrong-typed fields default to empty rather than raising —
    a single broken entry shouldn't sink the whole brief list.
    """
    return {
        "subticket_id": str(entry.get("subticket_id") or "").strip(),
        "relevant_files": _list_of_dicts(entry.get("relevant_files")),
        "related_symbols": _list_of_dicts(entry.get("related_symbols")),
        "prior_facts": _list_of_str(entry.get("prior_facts")),
        "gotchas": _list_of_str(entry.get("gotchas")),
    }


def parse(raw: str) -> list[dict]:
    """Extract the brief list from ``raw`` model output.

    Returns ``[]`` when nothing parsable is found — caller decides
    whether to retry or proceed with a thin context. Parsing is layered:

    1. Strip a markdown ```json fence if present.
    2. Try ``json.loads`` on the remainder (the happy path).
    3. Salvage: find the first balanced ``[...]`` and parse that.

    Any non-list result, or a list with non-dict entries, is filtered.
    """
    if not raw:
        return []
    text = _strip_code_fence(raw.strip())

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _find_balanced_array(text)
        if salvaged is None:
            return []
        try:
            data = json.loads(salvaged)
        except json.JSONDecodeError:
            return []

    if not isinstance(data, list):
        return []
    return [_coerce_brief(e) for e in data if isinstance(e, dict)]


__all__ = ["parse"]
