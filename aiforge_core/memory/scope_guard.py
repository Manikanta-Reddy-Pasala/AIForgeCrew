"""Deterministic guard on the GLOBAL memory scope.

Global is the highest-privilege scope in the system: a global learning is
injected into EVERY turn, for EVERY repository, as a mandatory rule. The spec
the scope classifier is given says so plainly — global means "true for every
repository", and it "must NOT mention any specific repo, library, service,
file, or function".

In practice global was the scope things fell into, not one they earned. Three
paths handed it out for free:

* ``md_store._scope.classify_scope`` — no repo hint and no topic hint fell
  back to ``global``;
* ``okf.author`` — an EMPTY scope string mapped to ``global``, as did a
  ``repo``-scoped learning whose repo was unknown;
* neither path checked the classifier's own rule about naming a file.

The result on a live install: 16 of 36 global learnings named a specific file
— ``calc.py`` seven times, ``demo.py`` four — all artifacts of benchmark runs.
Every chat turn, whatever it was about, opened with "All arithmetic functions
in calc.py must include edge-case handling" presented as a mandatory rule.

This module is the one place that decides whether a fact may be global, so
both writers agree and neither can drift. It is pure and deterministic: no
model call, no I/O.
"""
from __future__ import annotations

import re

# A concrete artifact reference — the thing the classifier spec forbids in a
# global rule. Deliberately narrow: a bare word like "config" is not evidence,
# a dotted filename or a path segment is.
_FILE_RE = re.compile(
    r"\b[\w][\w.-]*\.(?:py|java|kt|js|jsx|ts|tsx|go|rs|rb|php|c|h|cpp|hpp|cs|"
    r"sql|sh|bash|yaml|yml|json|toml|ini|xml|md|txt|csv|proto|gradle|tf)\b",
    re.IGNORECASE)
_PATH_RE = re.compile(r"(?:^|[\s(\"'`])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+")
# A dotted or ::-joined symbol path (com.foo.Bar, module::thing, Foo.bar())
_SYMBOL_RE = re.compile(r"\b\w+(?:::\w+|\.\w+){2,}\b|\b\w+\.\w+\(\)")


# Where a demoted fact lands when no repo is recorded. It must be a REAL
# scope slug: okf.store._scope_of maps anything it does not recognise back to
# "" (global), so a plain marker like "unscoped" would silently leave the node
# exactly where it was.
UNSCOPED = "repo:unscoped"


def names_specific_artifact(text: str) -> str:
    """Return the first concrete file/path/symbol named in ``text``, or ``""``.

    Used as evidence that a fact is about ONE codebase and therefore cannot be
    a global rule, however the classifier labelled it.
    """
    body = text or ""
    for rx in (_FILE_RE, _PATH_RE, _SYMBOL_RE):
        m = rx.search(body)
        if m:
            return m.group(0).strip(" (\"'`")
    return ""


def may_be_global(text: str) -> bool:
    """True when ``text`` is allowed to hold the global scope.

    A fact naming a concrete artifact is never global — that is the
    classifier's own stated rule, enforced here rather than trusted to the
    model (or to a fallback branch that never asked a model at all).
    """
    return not names_specific_artifact(text)


def demote_reason(text: str) -> str:
    """Short human/log explanation for a refused global, or ``""`` if allowed."""
    art = names_specific_artifact(text)
    return f"names {art!r} — specific to one codebase, not a global rule" if art else ""


__all__ = ["UNSCOPED", "names_specific_artifact", "may_be_global",
           "demote_reason"]
