"""Somebody's own machine is not the fleet's knowledge.

Two rules, both narrow on purpose. This stage exists to stop the obvious
personal note travelling, not to be a privacy classifier: a broad rule here
silently starves the fleet, and a fleet that learns nothing is a worse failure
than an over-shared note about a dotfile.
"""
from __future__ import annotations

import re

from aiforge_core.memory.sync.redact import _text

# The scope values that mean "about this machine and this user". Anything
# else — global, project, unset — syncs.
_LOCAL_SCOPES = {"local", "personal", "private"}

# A path under a home directory. Matched on SHAPE rather than against
# ``Path.home()``: a note may name another machine's home, and the rule is about
# the kind of reference, not about whose home it happens to be.
_HOME_PATH = re.compile(r"(?:/Users/|/home/|~/)[\w.@-]+", re.IGNORECASE)

# Signals that a note is about the codebase rather than about a machine.
_PROJECT = re.compile(
    r"`[^`\n]+`"                                                  # inline code
    r"|\b[\w-]+\.(?:py|ts|tsx|java|go|rs|sql|ya?ml|json|sh)\b"     # a filename
    r"|\b[A-Za-z]+(?:[A-Z][a-z]+)+\b"                             # CamelCase
    r"|\b\w+\(\)"                                                 # a call
)


def check(node: dict) -> tuple[str, str]:
    """Personal notes: a local scope, or a note whose only referent is a home path."""
    scope = str((node.get("meta") or {}).get("scope") or "").strip().lower()
    if scope in _LOCAL_SCOPES:
        return "private.scope", f"the note is scoped {scope}, so it stays on this machine"

    text = _text.text_of(node)
    if _HOME_PATH.search(text) and not _PROJECT.search(text):
        # A home path ALONGSIDE project signal is ordinary knowledge — "the venv
        # is at ~/.venv but the fix is in loop.py" — and must keep syncing. A
        # home path that is the whole note is somebody's own setup.
        return ("private.home_path",
                "the note is only about a path in a home directory")
    return "", ""


__all__ = ["check"]
