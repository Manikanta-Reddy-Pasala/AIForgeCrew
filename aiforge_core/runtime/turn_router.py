"""Is this message a follow-up in a chat that has already answered once?

This module used to hold a second thing: a cheap classifier call (the triage
model, SIMPLE or COMPLEX) that downgraded a Team turn to the single agent when
it judged the follow-up small. It is gone. Team is the user's explicit pick
for the message, and a helper model that sees a few truncated lines is not
asked to overrule it: a Team turn runs the team.
"""
from __future__ import annotations


def is_followup(history) -> bool:
    """True when this session already produced an assistant turn — i.e. the
    pipeline (or agent) has run at least once and this is a follow-up."""
    try:
        return any((m or {}).get("role") == "assistant" for m in (history or []))
    except Exception:  # noqa: BLE001
        return False


__all__ = ["is_followup"]
