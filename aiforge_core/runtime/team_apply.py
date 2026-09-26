"""Does the user want the team's result ON the branch they have checked out?

A team run works on its own ``aiforge/*`` branch (team_workspace); only an
explicit request in the user's CURRENT message fast-forwards their branch —
never a description ("the test fails on my branch") or an old message.
"""
from __future__ import annotations

import re

# Only an explicit imperative in the CURRENT message puts the result on the
# user's checked-out branch: "commit it to my branch", "merge it into main",
# "apply it to my branch", "fast-forward". A description ("the test fails on
# my branch, fix it") is not a request, and an old message never counts.
# The verb must be framed as a request: at the start of a sentence / after a
# comma, after "and/then/please/also/just/now", or after "can you / could you
# / would you / I want you to". Never after "error:" or inside quotes.
_IMPERATIVE_AT = (r"(?:^|[.;!?\n]\s*|,\s*|\b(?:and|then|please|also|just|now)"
                  r"\s+|\b(?:can|could|would|will)\s+you\s+|\bI\s+(?:want|need|"
                  r"would\s+like|'d\s+like)\s+you\s+to\s+)(?:please\s+)?")
_OBJ = r"(?:(?:it|this|them|that|the\s+(?:result|changes?|fix|work|branch))\s+)?"
_DEST = (r"(?:main|master|develop|trunk|(?:my|the\s+current|this|the\s+checked"
         r"[- ]out|our)\s+(?:current\s+)?branch)\b")
_APPLY_RE = re.compile(
    _IMPERATIVE_AT + r"(?P<v>"
    r"(?:apply|commit|merge|push|land|put)\s+" + _OBJ
    + r"(?:directly\s+|straight\s+)?(?:to|on|onto|into|in)\s+" + _DEST
    + r"|fast[- ]forward\b(?!\s+(?:fails?|failed|failing|is|was|does|did|"
    r"doesn'?t|didn'?t|isn'?t|error|errors|broke|breaks)\b))", re.I | re.M)
# "don't merge it", "do not fast-forward", "leave main alone", "keep it on
# its own branch" — an explicit NO, which lifts an earlier turn's request.
_NO_APPLY_RE = re.compile(
    r"\b(?:do\s+not|don[’']?t|never|no\s+need\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:merge|apply|fast[- ]forward|commit\s+(?:it\s+|this\s+)?(?:to|on|into)"
    r"|push|land|touch\s+(?:main|master|my\s+branch))\b|"
    r"\bleave\s+(?:main|master|my\s+branch)\s+alone\b|"
    r"\bkeep\s+(?:it|this|the\s+(?:result|work))\s+on\s+(?:a|the|its\s+own|"
    r"a\s+separate|the\s+new)\s+branch\b", re.I)
# Quoted / pasted text is not the user's request.
_QUOTED = re.compile(r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”", re.S)
_APPLY_NEG = re.compile(r"(?:\bdo\s+not|\bdon[’']?t|\bnever|\bnot|\bno)\s+"
                        r"(?:\w+\s+){0,2}$", re.I)


def wants_apply(texts) -> bool:
    """True when the CURRENT message (``texts[0]``, or a plain string) asks
    for the result on the user's branch — see :data:`_APPLY_RE`."""
    cur = texts if isinstance(texts, str) else next(iter(texts or ()), "")
    cur = str(cur or "").split("\n\n---\n[Interpreted request")[0]
    cur = _QUOTED.sub(" ", cur)
    return any(not _APPLY_NEG.search(cur[:m.start("v")])
               for m in _APPLY_RE.finditer(cur))


def refuses_apply(text) -> bool:
    """The current message explicitly says NOT to put the result on the
    user's branch (see :data:`_NO_APPLY_RE`)."""
    cur = _QUOTED.sub(" ", str(text or "").split(
        "\n\n---\n[Interpreted request")[0])
    return bool(_NO_APPLY_RE.search(cur))


__all__ = ["refuses_apply", "wants_apply"]
