"""Model-generated chat titles.

A fresh session is titled "New chat" and then provisionally set to a truncated
first message. After the first turn we ask the model for a concise, human title
so the sidebar reads well. Best-effort + bounded: any failure leaves the
provisional title in place.
"""
from __future__ import annotations

import re

_BAD = re.compile(r'^["\'`\s]+|["\'`\s]+$')


def suggest_title(prompt: str, role: str = "chat") -> str:
    """Concise 3-6 word title for a chat opened with ``prompt``. "" on failure
    (caller keeps the provisional title)."""
    text = (prompt or "").strip()
    if not text:
        return ""
    try:
        from aiforge_core.llm.client import complete
        out = complete(role, [
            {"role": "system", "content":
                "You generate a short, specific title for a chat. Reply with "
                "ONLY the title: 3-6 words, Title Case, no quotes, no trailing "
                "punctuation, no prefix like 'Title:'."},
            {"role": "user", "content": text[:1500]},
        ], max_tokens=20, temperature=0.0)
    except Exception:  # noqa: BLE001
        return ""
    if not out:
        return ""
    # First non-empty line; drop a 'Title:' prefix, THEN surrounding quotes.
    line = next((ln for ln in out.splitlines() if ln.strip()), "").strip()
    line = re.sub(r"^(title|chat)\s*[:\-]\s*", "", line, flags=re.I)
    line = _BAD.sub("", line)
    return line[:60]


__all__ = ["suggest_title"]
