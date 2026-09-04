"""Shared destructive-delete detection.

Policy: agents act autonomously for everything that unblocks the job
(install, build, run, edit, move, commit) — EXCEPT deleting files / data,
which must be confirmed by the user first. This module centralises the
pattern match used by both the conversational chat agent (``run_command``)
and the team Doer (``bash``). Override the policy per-process with
``AIFORGE_ALLOW_DELETE=1`` (or the chat-specific ``AIFORGE_CHAT_ALLOW_DELETE``).
"""
from __future__ import annotations

import os
import re

_DELETE_PATTERNS = [
    # BOUNDED, and the inner class requires a letter. `(-[a-z]*\s+)*` let an
    # option match as just "-" plus spaces, so a long run of dashes and spaces
    # could be split many ways before the match failed. Nobody passes rm more
    # than a few option groups.
    r"\brm\s+(?:-[a-z]+\s+){0,5}",   # rm, rm -rf, rm -f …
    r"\brmdir\b", r"\bunlink\b",
    r"\bgit\s+clean\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+branch\s+-D\b",
    r"\bfind\b.*-delete\b",
    r"\bdrop\s+(table|database)\b",
    r"\btruncate\s+table\b",
    r"\btruncate\s+-",                # coreutils: truncate -s 0 file
    # `dd ... of=<file>` overwrites/truncates the target (raw disk OR a file);
    # of=/dev/null is a harmless sink so exclude it.
    r"\bdd\b.*\bof=(?!/dev/null\b)",
    r"\bcp\s+/dev/null\s",           # cp /dev/null file → truncates file
    r">\s*/dev/sd",                   # writing over a raw disk
    # NOTE: plain `>` output redirection (`cat x > out.txt`, `npm build > log`)
    # is creation/overwrite, NOT a delete — and the autonomous Doer has no
    # approval path, so flagging it would hard-fail every redirect. Not matched.
    r"\bmkfs\b", r"\bshred\b",
    r"\bdocker\s+(rm|rmi|volume\s+rm|system\s+prune)\b",
    r"\bkubectl\s+delete\b",
]
_COMPILED = [re.compile(p) for p in _DELETE_PATTERNS]

REFUSAL = (
    "Refused: this command deletes files/data. The agent does every other "
    "operation autonomously but must ASK before deleting. Stop and ask the "
    "user to confirm this exact command; only re-run it after they agree."
)


def is_destructive_delete(cmd: str) -> bool:
    if not cmd:
        return False
    low = cmd.lower()
    return any(p.search(low) for p in _COMPILED)


def allow_delete(env_keys: tuple[str, ...] = ("AIFORGE_ALLOW_DELETE",)) -> bool:
    for k in env_keys:
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False
