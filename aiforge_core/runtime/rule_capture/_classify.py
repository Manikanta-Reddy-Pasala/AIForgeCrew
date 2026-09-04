"""The ONE capped LLM classify pass: strict-JSON extraction + parse + fail-open.

Split out of the former single ``rule_capture`` module VERBATIM. ``classify``
resolves ``_llm_complete`` through the package so an ``rc._llm_complete``
monkeypatch (tests) reaches it exactly as it did when everything lived in one
module.
"""
from __future__ import annotations

import json
import os
import re

from ._base import (
    _VALID_CATEGORIES,
    _VALID_SCOPES,
    _disabled,
    _min_conf,
    _none,
    log,
)


# ─────────────────────────── classify ───────────────────────────────

_SYS = (
    "You are a STRICT classifier that detects whether a user's chat message "
    "carries something the assistant should REMEMBER and apply later.\n\n"
    "Classify into exactly one category:\n"
    "- \"rule\": a standing directive / instruction about how to behave "
    "(\"always use yarn\", \"commit directly, the machine has access\", "
    "\"never force-push\").\n"
    "- \"memory\": a durable fact/preference to recall later "
    "(\"the staging DB is at db.staging\", \"my name is Sam\").\n"
    "- \"feedback\": a correction/preference on prior behaviour, softer than a "
    "hard rule (\"that was too verbose\", \"prefer shorter commits\").\n"
    "- \"none\": an ordinary task/question with nothing to remember.\n\n"
    "Also choose a scope:\n"
    "- \"global\": applies everywhere, all repos/sessions.\n"
    "- \"project\": applies to THIS repo only.\n"
    "- \"session\": applies to THIS conversation only.\n\n"
    "Default to \"project\" when the user references this repo/folder, "
    "\"global\" for universal directives, \"session\" for a one-off.\n\n"
    "Set \"task_present\" true when the message ALSO asks you to DO something "
    "now (build/fix/run/answer) in addition to stating the rule; false when it "
    "is PURELY a rule/fact/correction with no action requested.\n\n"
    "For \"rule\" or \"feedback\" ONLY: if the rule is scoped to a specific "
    "topic (e.g. deploys, a specific tool, a specific kind of file) rather "
    "than a universal directive, set \"triggers\" to 1-3 short lowercase "
    "topic words; leave it an empty list [] when the rule should ALWAYS "
    "apply regardless of topic.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{\"category\":\"rule|memory|feedback|none\",'
    '\"scope\":\"global|project|session\",'
    '\"canonical\":\"<cleaned one-line directive/fact>\",'
    '\"confidence\":0.0-1.0,\"task_present\":true|false,'
    '\"triggers\":[]}'
)


def _llm_complete(role: str, messages: list[dict], **kw) -> str:
    from aiforge_core.llm.client import complete
    return complete(role, messages, **kw)


def _next_string_state(ch: str, esc: bool) -> tuple[bool, bool]:
    """Advance the inside-a-string scan by one char → ``(still_in_string, esc)``.
    A backslash escapes the next char; an unescaped quote closes the string."""
    if esc:
        return True, False
    if ch == "\\":
        return True, True
    return ch != '"', False


def _first_balanced_span(text: str, start: int) -> str | None:
    """The ``{...}`` substring starting at ``start``, brace-matched with string
    awareness so braces inside a string value do not close the object early.
    None when it never balances."""
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            in_str, esc = _next_string_state(ch, esc)
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _extract_json(text: str) -> dict | None:
    """First balanced {...} object → dict, or None. String-aware brace match
    so braces inside string values don't confuse it."""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    span = _first_balanced_span(text, start)
    if span is None:
        return None
    try:
        obj = json.loads(span)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _parse_classification(raw: str) -> dict | None:
    obj = _extract_json(raw)
    if obj is None:
        return None
    cat = str(obj.get("category", "")).strip().lower()
    scope = str(obj.get("scope", "")).strip().lower()
    if cat not in _VALID_CATEGORIES:
        return None
    if scope not in _VALID_SCOPES:
        scope = "session"
    canonical = str(obj.get("canonical") or "").strip().replace("\n", " ")
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    task_present = obj.get("task_present")
    if not isinstance(task_present, bool):
        task_present = True
    triggers_raw = obj.get("triggers") or []
    if not isinstance(triggers_raw, list):
        triggers_raw = []
    # Restrict to a charset safe for BOTH storage formats: the inline
    # "[triggers: a, b]" bullet (chat_agent._BULLET_TRIGGERS_RE) and the
    # "triggers: [a, b]" YAML frontmatter (_write_repo_rule / repo_rules).
    # Chars like ] , : # * { } " would corrupt one or the other — a
    # corrupted frontmatter parse drops triggers and silently flips a gated
    # rule to always-on, so strip everything outside [a-z0-9 _-].
    triggers = [re.sub(r"[^a-z0-9 _-]", "", str(t).lower()).strip()
                for t in triggers_raw if isinstance(t, str) and t.strip()][:3]
    triggers = [t for t in triggers if t]  # drop anything that sanitized to empty
    triggers = [t for t in triggers if re.search(r"[a-z0-9]", t)]  # drop pure-punctuation junk
    return {"category": cat, "scope": scope, "canonical": canonical,
            "confidence": conf, "task_present": task_present,
            "triggers": triggers}


def classify(message: str, *, repo: str | None = None,
             session_id=None) -> dict:
    """ONE capped LLM call → a classification dict. FAILS OPEN: any
    error / non-JSON / unknown category / below-threshold confidence →
    ``{"category": "none", ...}``. The kill-switch env
    ``AIFORGE_RULE_CAPTURE_DISABLE=1`` short-circuits to none."""
    # unused, deliberately: classification is per-message; repo/session are the caller's context, not inputs.
    del repo, session_id
    from aiforge_core.runtime import rule_capture as _rc
    if _disabled() or not (message or "").strip():
        return _none()
    role = os.environ.get("AIFORGE_RULE_CAPTURE_ROLE", "enhancer")
    try:
        timeout = int(os.environ.get("AIFORGE_RULE_CLASSIFY_TIMEOUT_S", "15"))
    except ValueError:
        timeout = 15
    try:
        raw = _rc._llm_complete(
            role,
            [{"role": "system", "content": _SYS},
             {"role": "user", "content": message.strip()[:4000]}],
            max_tokens=250, temperature=0.0, timeout_s=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.classify llm error (none): %s", exc)
        return _none()
    c = _parse_classification(raw or "")
    if c is None:
        return _none()
    if c["category"] == "none" or not c["canonical"]:
        return _none()
    if c["confidence"] < _min_conf():
        return _none()
    return c
