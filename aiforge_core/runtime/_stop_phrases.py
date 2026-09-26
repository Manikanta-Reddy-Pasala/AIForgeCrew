"""Read a typed message as "stop the run" or "replace the run" — or neither.

A bare word search was too eager. "add a Cancel button" ended every
background watch, "how do I kill the process on :3000" stopped a
scheduled run, and "don't forget the README" cut a running build.

Only an imperative aimed at the run counts: the sentence starts with the
verb (after a little filler such as "ok", "please", "can you"), and what
follows is the run itself ("it", "that", "the watch", "everything"),
nothing, or a new clause. Text in code spans and quotes is ignored, and a
question ("how do I kill ...") never counts.
"""
from __future__ import annotations

import re

_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
# Paired quotes only. An apostrophe inside "don't" is not a quote.
_QUOTE_RE = re.compile(
    r"(?:(?<=\s)|^)(?:\"[^\"\n]*\"|“[^”\n]*”|'[^'\n]*')(?=[\s.,;:!?)]|$)")
_SENTENCE_RE = re.compile(r"[.!?;\n]+")

# Politeness and hesitation before the verb.
_FILLER = (
    r"(?:(?:ok(?:ay)?|hey|no|nope|nah|actually|just|please|pls|plz|kindly|"
    r"wait|hmm+|oh|ah|right|alright|now|so|and|but|never\s*mind|nvm|"
    r"that'?s\s+enough|enough|can\s+you|could\s+you|would\s+you|will\s+you|"
    r"you\s+can|go\s+ahead\s+and|i\s+want\s+you\s+to|i\s+need\s+you\s+to|"
    r"let'?s)[\s,:\-]*)*"
)
_CUT_VERB = r"(?:stop|cancel|abort|halt|kill|drop|end|terminate)"
_RUN_NOUN = (
    r"(?:run|runs|watch|watches|watcher|job|jobs|task|tasks|agent|loop|"
    r"schedule|scheduler|poll|polling|monitor|monitoring|check|checks|"
    r"work|build|process(?:es)?\s+you\s+started|command|script|wait)"
)
_RUN_OBJECT = (
    r"(?:it|that|this|them|everything|all(?:\s+of\s+(?:it|them))?|"
    r"(?:the|this|that|your|my|these|those|all(?:\s+the)?|any)\s+"
    r"(?:(?:background|scheduled|current|running|pipeline|ci|cron)\s+)*"
    + _RUN_NOUN + r"|"
    r"running|working)"
)
_TAIL = r"(?:\s+(?:now|right\s+now|immediately|please|for\s+now|already))*"
# After the verb (and object) the sentence ends, or a new clause starts.
# A bare "-" is a flag ("kill -9"), so a dash counts only between spaces.
_END = r"\s*(?:$|[,:]|\s[-\u2013\u2014]\s|\s+(?:and|then|so|&)\b)"
# "stop watching the pipeline", "cancel the scheduled deploy for 9am":
# the run is named outright, so whatever follows does not matter.
_OPEN_OBJECT = (
    r"(?:watching|polling|monitoring|checking|waiting|"
    r"(?:the\s+|this\s+|that\s+|my\s+|your\s+)?scheduled\s+\w+)\b"
)
# "stop the watch on the pipeline": a run noun may take a place or target.
_WHERE = r"(?:\s+(?:on|for|of|in|from|at|against)\s+\S.*)?"

_CUT_RE = re.compile(
    r"^" + _FILLER + _CUT_VERB + r"(?:"
    r"\s+" + _OPEN_OBJECT + r"|"
    r"(?:\s+" + _RUN_OBJECT + _WHERE + r")?" + _TAIL + _END + r")",
    re.IGNORECASE,
)

# A replacement: a sentence-initial imperative that throws the current work
# away ("drop the sleep", "skip that", "forget the extra log"), or an
# explicit change of plan anywhere ("... instead", "scratch that").
_REPLACE_START_RE = re.compile(
    r"^" + _FILLER
    + r"(?:stop|cancel|abort|halt|kill|drop|skip|quit|forget|scrap|ditch|"
    r"switch\s+to|undo|revert)"
    r"(?:$|\s+(?:it|that|this|them|everything|all|the|about|those|these|"
    r"your|my|any|now|watching|waiting|running|polling)\b|[,:])",
    re.IGNORECASE,
)
_REPLACE_ANY_RE = re.compile(
    r"\b(?:instead|scratch\s+that|never\s*mind|nvm|no\s+longer|hold\s+on|"
    r"wait,?\s+no|actually,?\s+no|forget\s+(?:it|that|this|about\s+(?:it|that))|"
    r"(?:don'?t|do\s+not)\s+(?:do|run|continue|proceed|bother|finish|keep|"
    r"build|deploy|start)\b)",
    re.IGNORECASE,
)

# Aimed at work the agent started to keep running: "stop the server", "kill
# the dev servers", "stop the background jobs", "stop everything".
_BACKGROUND_OBJECT = (
    r"(?:everything|all(?:\s+of\s+(?:it|them))?|"
    r"(?:the|this|that|your|my|these|those|all(?:\s+the)?|any)\s+"
    r"(?:(?:background|dev|web|local|running|api|preview)\s+)*"
    r"(?:servers?|services?|daemons?|background\s+\w+|"
    r"processes)|background\s+\w+)"
)
_BACKGROUND_RE = re.compile(
    r"^" + _FILLER + _CUT_VERB + r"\s+" + _BACKGROUND_OBJECT + _WHERE + _TAIL
    + _END, re.IGNORECASE)

_QUESTION_START_RE = re.compile(
    r"^(?:how|what|why|when|where|which|who|whose|is|are|was|were|does|did|"
    r"do\s+(?:i|we|you)|should|shall|may|might)\b",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    """The message with code spans and quoted strings blanked out."""
    out = _CODE_RE.sub(" ", text or "")
    return _QUOTE_RE.sub(" ", out)


def _sentences(text: str):
    for raw in _SENTENCE_RE.split(_plain(text)):
        s = raw.strip().strip("*_>").strip()
        if s:
            yield s


def _is_question(sentence: str) -> bool:
    """ "how do I kill ..." asks about stopping; it does not ask to stop.
    "can you stop it?" is a request and is not caught here."""
    return bool(_QUESTION_START_RE.match(sentence))


def cuts_run(text: str) -> bool:
    """True when ``text`` explicitly tells the running work to stop."""
    for s in _sentences(text):
        if _is_question(s):
            continue
        if _CUT_RE.match(s):
            return True
    return False


def cuts_background(text: str) -> bool:
    """True when ``text`` explicitly stops work left running in the
    background (a server, a background job) or everything."""
    for s in _sentences(text):
        if not _is_question(s) and _BACKGROUND_RE.match(s):
            return True
    return False


def replaces_run(text: str) -> bool:
    """True when ``text`` stops the running work or swaps it for another."""
    if cuts_run(text):
        return True
    for s in _sentences(text):
        if _is_question(s):
            continue
        if _REPLACE_START_RE.match(s) or _REPLACE_ANY_RE.search(s):
            return True
    return False
