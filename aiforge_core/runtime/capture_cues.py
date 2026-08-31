"""Single source for the "does this message carry a durable preference /
directive?" cue gate.

Three capture paths (rule_capture, preference_capture, chat_learner's LLM-down
fallback) each hand-rolled a near-identical cue regex. When they drifted, a
message caught by one path was missed by another — a recurring capture bug. This
is the ONE table they all share now.

An ordinary task ("fix the bug") has an imperative but NO preference cue, so it
never pays for an LLM classify call — the gate keeps the model off the hot path.
"""
from __future__ import annotations

import re

# Union of every cue the three former gates keyed on.
_CUE_RE = re.compile(
    r"\b("
    r"always|never|from now on|going forward|whenever|"
    r"remember|prefer|preference|rule|convention|setting|"
    r"i (?:prefer|want|like)|make sure to|"
    r"by default|default|use\s+\w+\s+(?:as|for)\b|set\s+\w+|"
    r"my (?:preference|setting|convention|default)|"
    r"don'?t|do not|auto[-\s]?approve|auto[-\s]?commit|"
    r"without asking|stop asking|no need to ask|commit directly|"
    r"for (?:all|every|each|this|the)"
    r")\b",
    re.IGNORECASE)


def has_cue(text: str) -> bool:
    """True when ``text`` carries a preference/directive cue worth an LLM
    classify call. Shared by every capture path."""
    return bool(_CUE_RE.search(text or ""))


__all__ = ["has_cue"]
