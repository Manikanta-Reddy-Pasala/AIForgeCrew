"""Model-generated chat titles.

A fresh session is titled "New chat" and then provisionally set to a truncated
first message. After the first turn we ask the model for a concise, human title
so the sidebar reads well. Best-effort + bounded: any failure leaves the
provisional title in place.
"""
from __future__ import annotations

import re

_BAD = re.compile(r'^["\'`\s]+|["\'`\s]+$')

# Leading filler to strip so the title starts on the SUBJECT, not the verb.
_LEAD = re.compile(
    r"^(please\s+|kindly\s+|can you\s+|could you\s+|would you\s+|i (?:want|need|"
    r"would like)\s+(?:you\s+)?(?:to\s+)?|help me(?:\s+to)?\s+|let'?s\s+)+",
    re.I)
_VERB = re.compile(
    r"^(build|create|write|make|implement|develop|add|generate|fix|explain|"
    r"analyse|analyze|summar[iy][sz]e|describe|review|design|set ?up|refactor|"
    r"debug|investigate|update)\s+(a|an|the|me|some)?\s*", re.I)
# Trailing clause that adds noise ("… with tests", "… across 3 modules").
_TAIL = re.compile(
    r"\s+(with|across|using|that|which|so that|in order to|and then|plus|,)\b.*$",
    re.I | re.S)


def provisional_title(text: str, max_words: int = 7, max_chars: int = 48) -> str:
    """A DECENT title from the first message WITHOUT an LLM — strips leading
    filler/verbs + trailing clauses, Title-Cases, caps length. Used instantly so
    a fresh chat reads well even before (or if) the model-title call succeeds,
    and as the fallback inside :func:`suggest_title`. Never empty for non-empty
    input."""
    t = (text or "").strip()
    if not t:
        return ""
    t = t.splitlines()[0]
    t = _LEAD.sub("", t)
    t = _VERB.sub("", t)
    t = _TAIL.sub("", t)
    t = t.strip(" .,:;-—")
    words = t.split()
    if words:
        t = " ".join(words[:max_words])
    if not t:
        t = (text or "").strip()
    if t and not any(c.isupper() for c in t):    # all-lower → Title Case it
        t = t.title()
    return t[:max_chars].strip() or (text or "").strip()[:max_chars]


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
    except Exception:  # noqa: BLE001 — endpoint down / contended → clean fallback
        return provisional_title(text)
    if not out:
        return provisional_title(text)
    # First non-empty line; drop a 'Title:' prefix, THEN surrounding quotes.
    line = next((ln for ln in out.splitlines() if ln.strip()), "").strip()
    line = re.sub(r"^(title|chat)\s*[:\-]\s*", "", line, flags=re.I)
    line = _BAD.sub("", line)
    return line[:60] or provisional_title(text)


__all__ = ["suggest_title"]
