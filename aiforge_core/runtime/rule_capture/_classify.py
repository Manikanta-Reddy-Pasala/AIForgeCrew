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
    "When the message states SEVERAL separate rules/facts (a numbered or "
    "bulleted list, or \"also ...\"), put the FIRST in the main fields and "
    "EACH other one in \"more\" as its own object with the same fields "
    "(category, scope, canonical, confidence, triggers). Never merge two "
    "rules into one canonical, and never drop one; keep exact values "
    "(paths, versions, flags) in the canonical text. \"more\" is [] when "
    "there is only one.\n\n"
    "Respond with STRICT JSON ONLY, no prose, no code fence:\n"
    '{\"category\":\"rule|memory|feedback|none\",'
    '\"scope\":\"global|project|session\",'
    '\"canonical\":\"<cleaned one-line directive/fact>\",'
    '\"confidence\":0.0-1.0,\"task_present\":true|false,'
    '\"triggers\":[],\"more\":[]}'
)

#: Most extra rules/facts kept from one message (beyond the first).
_MAX_MORE = 6


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


#: A directive about the current task only ("use the X workflow for this").
_ONE_OFF = re.compile(r"\b(?:for\s+this(?:\s+(?:one|task|bug|fix|change|ticket|request))?|this\s+time|"
                      r"right\s+now|for\s+now|in\s+this\s+(?:task|chat|request))\b", re.I)
#: Words that make it lasting.
_LASTING = re.compile(r"\b(?:always|never|every|each|from\s+now\s+on|remember|whenever|"
                      r"any\s+time|all\s+(?:my\s+)?(?:projects|repos|sessions)|going\s+forward|by\s+default)\b",
                      re.I)


def _one_off(canonical: str) -> bool:
    """A real chain stored "Use the pcb-bugfix workflow for this." as a
    PERMANENT project rule, injected as mandatory into every later session."""
    return bool(_ONE_OFF.search(canonical)) and not _LASTING.search(canonical)


def _parse_item(obj: dict, scope_default: str = "session") -> dict | None:
    """One {category, scope, canonical, confidence, triggers} object, cleaned;
    None when it is not a usable classification."""
    if not isinstance(obj, dict):
        return None
    cat = str(obj.get("category", "")).strip().lower()
    scope = str(obj.get("scope", "")).strip().lower()
    if cat not in _VALID_CATEGORIES:
        return None
    if scope not in _VALID_SCOPES:
        scope = scope_default
    canonical = str(obj.get("canonical") or "").strip().replace("\n", " ")
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
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
    if cat in ("rule", "feedback") and scope != "session" and _one_off(canonical):
        scope = "session"
    return {"category": cat, "scope": scope, "canonical": canonical,
            "confidence": conf, "triggers": triggers}


def _parse_classification(raw: str) -> dict | None:
    obj = _extract_json(raw)
    if obj is None:
        return None
    first = _parse_item(obj)
    if first is None:
        return None
    task_present = obj.get("task_present")
    if not isinstance(task_present, bool):
        task_present = True
    first["task_present"] = task_present
    # A message with several rules ("1. ... 2. ...") used to keep only the
    # first: the rest were never stored, so a later session did not know them.
    more, seen = [], {first["canonical"].lower()}
    for extra in (obj.get("more") or [])[:_MAX_MORE] if isinstance(obj.get("more"), list) else []:
        item = _parse_item(extra, scope_default=first["scope"])
        if item is None or item["category"] == "none" or not item["canonical"]:
            continue
        if item["canonical"].lower() in seen:
            continue
        seen.add(item["canonical"].lower())
        item["task_present"] = task_present
        more.append(item)
    first["more"] = more
    return first


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
            max_tokens=700, temperature=0.0, timeout_s=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("rule_capture.classify llm error (none): %s", exc)
        return _none()
    c = _parse_classification(raw or "")
    if c is None:
        return _none()
    c["more"] = [m for m in c.get("more", []) if m["confidence"] >= _min_conf()]
    if c["category"] == "none" or not c["canonical"] or c["confidence"] < _min_conf():
        # The first slot can be "none" while a listed one is real: promote it.
        if not c["more"]:
            return _none()
        nxt, rest = c["more"][0], c["more"][1:]
        c = {**nxt, "more": rest}
    return c
