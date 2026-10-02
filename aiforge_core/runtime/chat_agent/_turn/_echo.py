"""A reply that only repeats the system's action log is not an answer.

Each earlier assistant turn is stored with a ``[did: file_read(a)✓, …]`` line the
SYSTEM adds, so the next turn remembers what was done. In a long chat the model
copies that format into its own reply ("Honest re-check — … [did: …]") and the
turn ends with a log where the result should be. The user sees a list of calls
and no work.
"""
from __future__ import annotations

import re

_LOG = re.compile(r"\[did:.*", re.S)

NUDGE = ("[loop guard — not the user] Your reply was only a log of earlier "
         "actions. The `[did: …]` line is added by the system; never write it. "
         "Do the work now: take the next action, or write the actual result "
         "for the user (what you found, what you changed, what the numbers "
         "are).")


def strip_action_log(text: str) -> "tuple[str, bool]":
    """``(clean_text, echo_only)``. Removes a ``[did: …]`` log from ``text``.
    ``echo_only`` is True when nothing of substance is left: the reply was the
    log, or a lead-in sentence ending in a colon that introduces it."""
    text = text or ""
    m = _LOG.search(text)
    if not m:
        return text, False
    clean = text[:m.start()].rstrip()
    return clean, (not clean) or clean.endswith(":") or len(clean) < 25
