"""One-shot hint when a CREATE … TRIGGER parse falls back to ``exp.Command``.

sqlglot-trigger boc15a (11-12): the agent wired ``_parse_trigger`` but kept a
second ``self._match(TokenType.TRIGGER)`` after ``_parse_create`` had already
consumed TRIGGER via ``CREATABLES``. Every identity stayed ``Command`` (6/25
for an hour). OpenCode's winning pattern: if ``create_token`` is CONSTRAINT and
the next token is TRIGGER, ``_advance()`` past TRIGGER and set ``create_token``
to TRIGGER; the body parser then starts at the *name* — never rematch TRIGGER.

Fires at most once per session when a pytest/run_tests observation shows that
fallback. ``AIFORGE_CHAT_PARSE_CONSUME_HINT=0`` turns it off.
"""
from __future__ import annotations

import os
import re

from aiforge_core.runtime.failure_signature import result_text

_NOTE = (
    "[harness — not the user] The failing assert shows CREATE … TRIGGER still "
    "parsed as ``exp.Command``. In sqlglot, ``_parse_create`` already matches "
    "the creatable keyword (TRIGGER, or CONSTRAINT when CONSTRAINT is in "
    "CREATABLES). If create_token is CONSTRAINT and the next token is TRIGGER, "
    "advance past TRIGGER and treat create_token as TRIGGER. The trigger body "
    "parser must start at the trigger *name* — do not call "
    "``_match(TokenType.TRIGGER)`` again (that rematch returns None and forces "
    "the Command fallback). Also parse REFERENCING OLD/NEW TABLE [AS] alias."
)

_PAT = re.compile(
    r"Command\(this=CREATE.*\bTRIGGER\b|"
    r"is not an instance of <class 'sqlglot\.expressions\.Create'>|"
    r"CREATE CONSTRAINT TRIGGER",
    re.I | re.S,
)


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_PARSE_CONSUME_HINT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def after_test(st, name, args, result) -> str:
    """Return the note once when a failed test run shows Command-for-TRIGGER."""
    if not _on() or getattr(st, "readonly_mode", False) or getattr(st, "plan_mode", False):
        return ""
    if getattr(st, "parse_consume_hint_shown", False):
        return ""
    if not isinstance(result, dict) or result.get("ok") is not False:
        return ""
    cmd = ""
    if isinstance(args, dict):
        cmd = str(args.get("cmd") or args.get("command") or "")
    if name != "run_tests" and "pytest" not in cmd:
        return ""
    text = result_text(result)
    if not text or not _PAT.search(text):
        return ""
    st.parse_consume_hint_shown = True
    return _NOTE
