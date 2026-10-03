"""'Yes, continue' means do the work, not describe it again.

Live: the user answered a plan with "yes continue" / "do it" and the agent
re-checked the tree, wrote "Nothing was written this turn … say start and I will
do step 1 in the next turn", and stopped. Said again, same result: a loop where
every turn ends with a request for permission the user has already given.

This module recognises a go-ahead message and a final that asks for one, and
finds the request the go-ahead refers to, so the zero-edit guard can hold such a
turn to the standard of a change request.
"""
from __future__ import annotations

import re

_OPENER = re.compile(
    r"^\s*(?:yes|yep|yeah|yup|y|ok(?:ay)?|sure|go(?:\s+ahead|\s+on)?|proceed|continue|"
    r"do\s+it|start|carry\s+on|keep\s+going|next|please\s+(?:do|continue|proceed)|"
    r"approved?|confirmed?|sounds\s+good|looks\s+good)\b", re.I)
_QUESTION = re.compile(r"\?\s*$")
_ASKS_PERMISSION = re.compile(
    r"\b(?:say|reply|tell\s+me|type|send|answer)\b[^.\n]{0,30}\b(?:start|go|yes|continue|proceed|go\s+ahead)\b"
    r"|\b(?:shall\s+i|should\s+i|do\s+you\s+want\s+me\s+to|want\s+me\s+to)\b[^.\n]{0,70}\b"
    r"(?:proceed|start|begin|continue|go\s+ahead|do\s+(?:it|this|that|step))"
    r"|\blet\s+me\s+know\s+(?:if|when)\b[^.\n]{0,50}\b(?:proceed|start|begin|continue)",
    re.I)
_ADMITS_NO_WORK = re.compile(
    r"nothing\s+(?:was|has\s+been|is)\s+(?:written|changed|done|implemented|executed)"
    r"|no\s+(?:file|edit|change)s?\s+(?:was|were|has\s+been|have\s+been)\s+(?:written|made|changed)"
    r"|(?:has|have)\s+(?:still\s+)?not\s+(?:been\s+)?(?:executed|written|implemented|done)"
    r"|in\s+the\s+next\s+turn|next\s+turns?,?\s+one\s+module", re.I)
_MAX_GO_AHEAD_CHARS = 200

NUDGE = ("[harness — not the user] The user ALREADY told you to go ahead. Do not "
         "ask for permission again, do not re-verify state you have already "
         "checked, and do not describe what you would do. Take step 1 NOW: make "
         "the edit with a tool call, then run the check, then continue with the "
         "next step. Only FINAL when the work is done (say what changed) or when "
         "something concrete blocks you (say exactly what).")


def is_go_ahead(text: str) -> bool:
    """A short 'yes / continue / do it / go ahead' message (optionally followed by
    a few words), not a question."""
    t = (text or "").strip()
    if not t or len(t) > _MAX_GO_AHEAD_CHARS or _QUESTION.search(t):
        return False
    return bool(_OPENER.match(t))


def asks_permission(text: str) -> bool:
    """The reply ends by asking the user to say 'start' / 'continue' / 'go ahead'."""
    return bool(_ASKS_PERMISSION.search((text or "")[-400:]))


def admits_no_work(text: str) -> bool:
    """The reply itself says nothing was done / the work will happen next turn."""
    return bool(_ADMITS_NO_WORK.search(text or ""))


def _is_user_ask(m: dict) -> bool:
    from .._context._compaction import _is_harness_note, _text_of
    if (m or {}).get("role") != "user":
        return False
    t = _text_of(m).strip()
    return bool(t) and not t.startswith(("OBSERVATION:", "<<AIFORGE")) \
        and not t.startswith("[HANDOFF") and not _is_harness_note(t)


def effective_goal(st) -> str:
    """The request a go-ahead refers to: the latest earlier user message that is
    not itself a go-ahead. The turn's own goal when it is not a go-ahead."""
    goal = (getattr(st, "goal", "") or "").strip()
    if not is_go_ahead(goal):
        return goal
    from .._context._compaction import _text_of
    for m in reversed(getattr(st, "convo", []) or []):
        if _is_user_ask(m):
            t = _text_of(m).strip()
            if t and t != goal and not is_go_ahead(t):
                return t
    return goal


def had_earlier_assistant_turn(st) -> bool:
    return any((m or {}).get("role") == "assistant"
               for m in (getattr(st, "convo", []) or [])[1:])
