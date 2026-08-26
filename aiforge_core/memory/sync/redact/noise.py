"""The idle-search class: what the user asked, not what the fleet learned.

A user asks the assistant something idle — a country, a definition, how to
centre a div. It becomes a capture, the capture becomes a node, and the node
syncs to every machine in the fleet forever. None of it is knowledge about the
work, and at volume it crowds out the knowledge that is.

Every threshold here is a named constant with an env override, because the right
value is discovered from the block log rather than argued about up front. Each
rule reports its own name for the same reason: an operator who sees
"noise.no_project_signal held back 40 notes" knows which dial to turn.

The project-signal test is deliberately generous. A false "this is project
knowledge" costs one extra synced note; a false negative means a real fact never
leaves the machine that learned it, and nothing ever tells anybody.
"""
from __future__ import annotations

import os
import re

from aiforge_core.memory.sync.redact import _text


def _threshold(env: str, default: int) -> int:
    try:
        return int(os.environ.get(env) or default)
    except ValueError:
        return default


def min_substance() -> int:
    """Characters of real content, once markdown furniture is stripped. Below
    this a node cannot carry a fact worth replicating."""
    return _threshold("AIFORGE_FILTER_MIN_SUBSTANCE", 80)


def min_words() -> int:
    """Words below which "is this about the work" cannot be judged from content
    at all, so the project-signal rule is the only one that can apply."""
    return _threshold("AIFORGE_FILTER_MIN_WORDS", 8)


# Anything that says a note is about this codebase.
#
# Split across TWO patterns because the flag matters: ``re.IGNORECASE`` turns
# ``[A-Z][a-z]+`` into "any letter", so a single combined case-insensitive regex
# made every ordinary English word look like CamelCase and therefore like
# project signal — every idle search passed the filter. Keep the case-sensitive
# half case-sensitive.
_SIGNAL_CASED = re.compile(
    r"`[^`\n]+`"                                              # inline code
    r"|\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"                          # camelCase
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"                  # CamelCase
    r"|\b[A-Z]{2,}[A-Z0-9_]*\b"                                # CONSTANT_CASE
)
_SIGNAL_ANY = re.compile(
    r"\b[\w-]+\.(?:py|ts|tsx|java|go|rs|sql|ya?ml|json|sh|md)\b"  # a filename
    r"|(?:^|\s)/[\w./-]{3,}"                                    # a path
    r"|\b\w+\(\)"                                               # a call
    r"|\b(?:error|exception|traceback|timeout|failed|regression|commit|"
    r"branch|deploy|endpoint|schema|migration|threshold|retry|backoff|"
    r"consumer|queue|rounds? off|rollback|env var)\b"
    r"|\b[0-9a-f]{7,40}\b",                                     # a sha
    re.IGNORECASE)

# A title that asks rather than states. Not blocked on its own — a question is a
# fine title when the body answers it — only when the body resolves nothing.
_QUESTION = re.compile(
    r"^\s*(?:what|who|where|when|why|how|which|is|are|does|do|can|should)\b"
    r"|\?\s*$", re.IGNORECASE)

# A body that is one dictionary fact about one proper noun. Matched on shape,
# since the entity is whatever the user happened to ask about.
_ENCYCLOPAEDIC = re.compile(
    r"\b(?:is a|is an|is the|are the|refers to|stands for|capital|population|"
    r"located in|founded in|born in|invented by)\b", re.IGNORECASE)

# A note carrying project signal is judged only against this floor, not against
# ``min_substance()``. Real knowledge is often terse — "MongoDbService is
# mandatory" is the most valuable kind of note in this codebase and would not
# survive an 80-character rule. Signal is the strong evidence; length is the
# weak evidence, and the weak one must not override the strong one.
_SIGNAL_FLOOR = 20


def _has_signal(text: str) -> bool:
    return bool(_SIGNAL_CASED.search(text) or _SIGNAL_ANY.search(text))


def check(node: dict) -> tuple[str, str]:
    """The first noise rule this node trips, or ``("", "")``.

    Ordered so the common case — a real note full of code spans — is decided by
    two regexes and one length comparison.
    """
    text = _text.text_of(node)
    body = _text.substance(node)
    title = _text.title_of(node) or (text.splitlines() or [""])[0]

    if _has_signal(text):
        if len(body) < _SIGNAL_FLOOR:
            return "noise.thin", "almost nothing in the note besides its title"
        return "", ""

    if len(body) < min_substance():
        return ("noise.thin",
                f"under {min_substance()} characters of content and nothing "
                "identifying the work")
    if len(text.split()) < min_words():
        return "noise.no_project_signal", "nothing in the note identifies the work"
    if _ENCYCLOPAEDIC.search(body):
        return ("noise.encyclopaedic",
                "the note reads as a general fact rather than something learned here")
    if _QUESTION.search(title):
        return "noise.unanswered", "a question whose body does not answer it"
    return "noise.no_project_signal", "nothing in the note identifies the work"


__all__ = ["check", "min_substance", "min_words"]
