"""Trimming a tool result to what fits in the model's context: the largest
text field is cut, at a line boundary where possible, and the rest kept."""
from __future__ import annotations

import json

_OBS_TEXT_KEYS = ("content", "text", "body", "markdown", "preview", "stdout")


def _largest_text_key(result: dict, raw_len: int, cap: int) -> str | None:
    """The text field big enough that trimming it alone gets us under ``cap``."""
    key = max((k for k in _OBS_TEXT_KEYS if isinstance(result.get(k), str)),
              key=lambda k: len(result[k]), default=None)
    return key if key and len(result[key]) > (raw_len - cap) else None


def _cut_at_structure(text: str, budget: int) -> str:
    """``budget`` chars of ``text``, cut at a structure boundary."""
    from aiforge_core.integrations import chonkie_text_adapter
    if chonkie_text_adapter.available():
        return chonkie_text_adapter.cut_at_structure(text, budget)
    # dep-free structural fallback: last paragraph boundary
    kept = text[:budget]
    nl = kept.rfind("\n\n")
    return kept[:nl] if nl > budget // 2 else kept


def _trimmed_json(result: dict, key: str, raw_len: int, cap: int) -> str:
    text = result[key]
    budget = max(500, len(text) - (raw_len - cap) - 200)
    kept = _cut_at_structure(text, budget)
    trimmed = dict(result)
    trimmed[key] = (kept + f"\n…[TRUNCATED at a structure "
                    f"boundary — {len(kept)} of {len(text)} chars "
                    "shown. The document CONTINUES: use read_lines "
                    "with an offset, or ask for a specific section.]")
    return json.dumps(trimmed)


def _smart_truncate_obs(result, cap: int) -> str:
    """Serialize a tool result to at most ``cap`` chars for the OBSERVATION.

    A plain ``json.dumps(result)[:cap]`` slices mid-sentence/mid-JSON —
    the model reads a broken tail and mis-handles long files/pages. When a
    content-read result exceeds the cap, cut its LARGEST text field at a
    STRUCTURE boundary (chonkie RecursiveChunker when installed) with an
    explicit continuation note, so the model knows the doc continues and
    how to get more. Falls back to the old blunt slice on any failure."""
    try:
        raw = json.dumps(result)
    except (TypeError, ValueError):
        raw = json.dumps(str(result))
    if len(raw) <= cap:
        return raw
    try:
        key = _largest_text_key(result, len(raw), cap) \
            if isinstance(result, dict) else None
        if key:
            out = _trimmed_json(result, key, len(raw), cap)
            if len(out) <= cap + 400:          # small tolerance for the note
                return out
    except Exception:  # noqa: BLE001 — smart cut is best-effort
        pass
    return raw[:cap]
