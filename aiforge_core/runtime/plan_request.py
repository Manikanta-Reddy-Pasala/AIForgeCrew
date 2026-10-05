"""A Simple-mode message that asks for a PLAN is planned, not carried out.

Live: the user typed "give me a plan for …" with the mode toggle on Simple
(approving an earlier plan had moved it there). The agent has every tool in
that mode, so it wrote the code. The words asked for a plan; the toggle said
"act"; the toggle won.

So an explicit ask for a plan runs that turn read-only, exactly as the Plan
mode does: the agent reads, writes the plan, and the user approves it to have
it carried out. Only the plain cases are caught — the ask has to be in the
opening of the message, and a message that also says to carry the plan out
("plan and implement", "follow the approved plan"), that declines one ("no
plan needed"), or that names a plan as a THING to build ("the pricing plan
page") is left to the mode the user chose.

``AIFORGE_CHAT_PLAN_REQUEST=0`` turns it off.
"""
from __future__ import annotations

import os
import re

#: The ask is looked for in the opening of the message only: a long brief or a
#: pasted document says "plan" somewhere without asking for one.
_HEAD_CHARS = 400

_POLITE = (r"(?:(?:please|pls|kindly|ok(?:ay)?|so|now|first|then|just|only|"
           r"can\s+you|could\s+you|would\s+you|will\s+you|let'?s|"
           r"i\s+want\s+you\s+to|i\s+need\s+you\s+to|you\s+should)[\s,]+)*")

_ASKS = re.compile(
    # "plan how to …", "please plan the migration"
    rf"^\s*{_POLITE}plan\s+(?!is\b|was\b|looks\b|seems\b|sounds\b|approved\b"
    r"|on\b|in\b|to\s+use\b|mode\b|b\b|tab\b|button\b|toggle\b)"
    # "give me a plan", "come up with an implementation plan", "need a plan"
    r"|\b(?:give|show|share|send|tell|write|draft|make|create|prepare|propose|"
    r"suggest|outline|come\s+up\s+with|need|want|get)\s+(?:me\s+|us\s+)?"
    r"(?:a|an|the|your|some)\s+(?:[\w-]+\s+){0,2}plan\b"
    # "plan first", "just a plan", "plan only", "only the plan"
    r"|\bplan\s+(?:it\s+|this\s+|that\s+)?(?:first|only)\b"
    r"|\b(?:just|only)\s+(?:a\s+|the\s+)?plan\b"
    # "plan before you implement"
    r"|\bplan\s+before\s+(?:you\s+)?(?:implement|cod|chang|writ|edit|start|do)"
    # "what is your plan", "what's the plan"
    r"|\bwhat(?:'s|\s+is|\s+would\s+be)\s+(?:your|the)\s+plan\b",
    re.IGNORECASE)

#: The message also wants the plan carried out, or is about one that exists.
_CARRY_OUT = re.compile(
    r"\b(?:carry\s+out|execute|implement|apply|follow|proceed\s+with|"
    r"go\s+ahead\s+with|as\s+per|according\s+to|continue\s+with|stick\s+to|"
    r"start\s+(?:on|with))\s+(?:the\s+|this\s+|that\s+|your\s+|my\s+|our\s+|"
    r"approved\s+|above\s+)*plan\b"
    r"|\bplan\b[^.?!\n]{0,40}(?:\band\b|\bthen\b|&)\s*(?:then\s+)?(?:implement|"
    r"execute|build|do\s+it|code|apply|carry|fix|make\s+the|write\s+the|"
    r"refactor|add|update|change|run|create|remove|delete|migrate|deploy|"
    r"start|proceed|edit|rename|move|test|commit|push|merge)\b"
    r"|\bapproved\s+plan\b"
    # "write the plan to PLAN.md": a file is asked for
    r"|\bplan\s+(?:to|into|in)\s+\S+\.\w{1,5}\b",
    re.IGNORECASE)

#: "no plan needed", "skip the plan", "don't plan".
_DECLINES = re.compile(
    r"\b(?:no|skip|without|don'?t|do\s+not|not)\s+"
    r"(?:(?:need|want|give\s+me|write|make|create)\s+)?"
    r"(?:a\s+|the\s+|any\s+|to\s+)?plan(?:ning)?\b",
    re.IGNORECASE)

#: A plan that is a thing in the product, not a plan of work.
_PLAN_AS_THING = re.compile(
    r"\b(?:pricing|subscription|billing|payment|floor|meal|data|tariff|rate|"
    r"insurance|workout|lesson|seating|savings|membership|service|phone|"
    r"mobile|query|execution|current|existing|selected|active|"
    r"user'?s?|customer'?s?|free|paid|pro|basic|premium)\s+plans?\b"
    r"|\bplans?\s+(?:page|model|table|entity|feature|tier|screen|api|endpoint|"
    r"class|component|field|column|file|doc|document|module|service|"
    r"selector|card|list)\b"
    r"|\bplans?\s+(?:in|on)\s+(?:stripe|paypal|razorpay|the\s+(?:db|database|"
    r"app|system|admin|dashboard|ui|settings))\b",
    re.IGNORECASE)

NOTICE = ("📋 You asked for a plan — planning only this turn, nothing is "
          "changed. Approve the plan to have it carried out.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_PLAN_REQUEST", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def asks_for_plan_only(text: str) -> bool:
    """The message asks for a plan and not for the work itself."""
    head = (text or "").strip()[:_HEAD_CHARS]
    if not head or not enabled() or not _ASKS.search(head):
        return False
    return not (_CARRY_OUT.search(head) or _DECLINES.search(head)
                or _PLAN_AS_THING.search(head))


def turns_plan_down(text: str) -> bool:
    """The message says to do the work, or that no plan is wanted."""
    head = (text or "").strip()[:_HEAD_CHARS]
    return bool(_CARRY_OUT.search(head) or _DECLINES.search(head))


__all__ = ["NOTICE", "asks_for_plan_only", "enabled", "turns_plan_down"]
