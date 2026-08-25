"""Model-generated chat titles.

A fresh session is titled "New chat" and then provisionally set to a truncated
first message. After the first turn we ask the model for a concise, human title
so the sidebar reads well. Best-effort + bounded: any failure leaves the
provisional title in place.
"""
from __future__ import annotations

import re

# Grouped explicitly — see the note in memory/okf/tiers.py.
# POSSESSIVE quantifiers (`++`, Python 3.11+ and this project requires >=3.11).
# `\W+$`-style strips backtrack super-linearly on input that does NOT match:
# the engine retries the run at every length before giving up. `++` never
# gives characters back, which is exactly right for a strip and turns the
# scan linear.
_BAD = re.compile(r'^["\'`\s]++|["\'`\s]++$')

# A reasoning model (triage role) emits chain-of-thought first; capped at a few
# tokens it leaks TRUNCATED CoT ("Thinking Process:", "The user is asking…") that
# must never become the title.
_THINK_TAG = re.compile(r"<think>.*?(?:</think>|$)", re.I | re.S)
_REASON_START = re.compile(
    r"^(thinking|thought|okay|ok\b|so\b|well\b|sure\b|let'?s|let me|first\b|"
    r"the user|i (?:need|should|think|'?ll|will|am|have|can|would)|"
    r"we (?:need|should|could)|here'?s|alright|now\b|hmm|to (?:answer|title|"
    r"generate|create)|reasoning|analysis|based on)\b", re.I)


def _title_like(line: str) -> bool:
    """A short label, not a sentence of reasoning."""
    if not line or line.endswith(":"):
        return False
    if _REASON_START.match(line):
        return False
    return 1 <= len(line.split()) <= 10


def _extract_title(out: str, prompt: str) -> str:
    """Pull a clean title out of a possibly-reasoning model dump; fall back to
    the deterministic provisional title when nothing title-like survives."""
    cleaned = _THINK_TAG.sub("", out or "")
    lines = []
    for ln in cleaned.splitlines():
        ln = re.sub(r"^(title|chat)\s*[:\-]\s*", "", ln.strip(), flags=re.I)
        ln = _BAD.sub("", ln)
        if ln:
            lines.append(ln)
    # A reasoning model concludes with the title LAST — prefer the last clean line.
    for ln in reversed(lines):
        if _title_like(ln):
            return ln[:60]
    return provisional_title(prompt)

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
                "punctuation, no prefix like 'Title:'. Do NOT think out loud or "
                "explain — output the title text and nothing else. /no_think"},
            {"role": "user", "content": text[:1500]},
        # Enough room for a reasoning model to finish any CoT AND still emit the
        # title on a final line — _extract_title then discards the CoT. (At 20
        # tokens the CoT was truncated and its preamble became the title.)
        ], max_tokens=64, temperature=0.0)
    except Exception:  # noqa: BLE001 — endpoint down / contended → clean fallback
        return provisional_title(text)
    if not out:
        return provisional_title(text)
    return _extract_title(out, text)


__all__ = ["suggest_title"]
