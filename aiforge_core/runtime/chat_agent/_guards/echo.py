"""A reply that only repeats the system's action log is not an answer.

Each earlier assistant turn used to be stored with a ``[did: file_read(a)✓, …]``
line the SYSTEM added, so the next turn remembered what was done. In a long chat
the model copied that format into its own reply ("Honest re-check — … [did: …]")
and the turn ended with a log where the result should be. The user saw a list of
calls and no work.

That line is gone from the history: what was done now lives in the session
action log, a harness note the model reads but does not own
(``runtime/action_log.py``). This guard stays as the safety net, for the old
format and for a reply that copies the new note.
"""
from __future__ import annotations

import re

# The old per-turn digest, and the session action log's own markers and
# placeholder (runtime/action_log.py): a reply never carries any of them.
_LOG = re.compile(r"\[did:.*|<<AIFORGE_ACTION_LOG>>.*|\[action log — not the user\].*"
                  r"|\(This turn ended without a written reply\.\).*", re.S)

NUDGE = ("[loop guard — not the user] Your reply was only a log of earlier "
         "actions. The action log is written by the system; never write one. "
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


class EchoGuard:
    """Strip the log from the final; if nothing else is left, send the model
    back (three times), then say plainly that no result came. Not a claim
    check, so it runs itself (see ``base.run_guards``)."""

    def check(self, st, step):
        _clean, _echo_only = strip_action_log(step.get("text") or "")
        if _clean != (step.get("text") or ""):
            step["text"] = _clean
            if _echo_only:
                st.echo_nudges = getattr(st, "echo_nudges", 0) + 1
                if st.echo_nudges <= 3:
                    yield {"type": "thought", "role": "system",
                           "text": "↺ the reply was only an action log — asking for "
                                   "the actual result"}
                    st.convo.append({"role": "user", "content": NUDGE})
                    return "continue"
                step["text"] = ("(I could not produce a result for this: I kept "
                                "listing actions instead of finishing. Say "
                                "\"continue\" and I will pick it up, or tell me what "
                                "to change.)")
        return None
