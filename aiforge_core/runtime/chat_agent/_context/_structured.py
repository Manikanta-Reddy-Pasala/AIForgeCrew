"""A typed summary of a condensed slice, in place of free text.

A free-text "4-12 bullets" summary lets a small model drop exactly what the
run needs after a condense: which tests still fail, what was tried and did not
work, what comes next. OpenHands' ``StructuredSummary`` fixes that by asking
for named fields. This module asks the same, parses and validates the answer,
and renders it as short labelled lines. An answer that does not parse into the
fields is refused (the caller keeps the deterministic breadcrumb), so a rambling
reply never replaces it.

``AIFORGE_COMPACT_STRUCTURED=0`` restores the free-text prompt and accepts any
text, as before.
"""
from __future__ import annotations

import json
import os
import re

#: field -> "str" (one line) or "list" (several lines)
FIELDS = {
    "goal": "str",
    "done_and_verified": "list",
    "pending": "list",
    "files_changed": "list",
    "failing_tests_errors": "list",
    "decisions": "list",
    "failed_approaches": "list",
    "next_step": "str",
}

_ALIASES = {
    "task": "goal", "objective": "goal", "user_goal": "goal",
    "done": "done_and_verified", "done_verified": "done_and_verified",
    "completed": "done_and_verified", "verified": "done_and_verified",
    "todo": "pending", "remaining": "pending", "open": "pending",
    "files": "files_changed", "changed_files": "files_changed",
    "files_modified": "files_changed", "files_edited": "files_changed",
    "errors": "failing_tests_errors", "failing_tests": "failing_tests_errors",
    "failing_tests_or_errors": "failing_tests_errors",
    "errors_verbatim": "failing_tests_errors", "failures": "failing_tests_errors",
    "failing": "failing_tests_errors",
    "decision": "decisions", "key_decisions": "decisions",
    "failed": "failed_approaches", "failed_attempts": "failed_approaches",
    "dead_ends": "failed_approaches", "tried_and_failed": "failed_approaches",
    "next": "next_step", "next_steps": "next_step", "next_action": "next_step",
}

_MAX_ITEMS = 10
_MAX_FILES = 20
_ITEM_CHARS = 300
_ERR_CHARS = 400
_LABELS = {
    "goal": "GOAL",
    "done_and_verified": "DONE AND VERIFIED",
    "pending": "PENDING",
    "files_changed": "FILES CHANGED",
    "failing_tests_errors": "FAILING TESTS / ERRORS (verbatim)",
    "decisions": "DECISIONS",
    "failed_approaches": "FAILED APPROACHES (do not repeat)",
    "next_step": "NEXT STEP",
}

SYSTEM = (
    "You compress an earlier slice of a coding-assistant conversation into a "
    "typed record the assistant relies on after the raw turns are dropped. "
    "Reply with ONE JSON object and nothing else (no markdown fences, no "
    "preamble), with exactly these keys:\n"
    '{"goal": "the user\'s task in one line",\n'
    ' "done_and_verified": ["work that is finished AND checked (a test passed, '
    'a file was confirmed)"],\n'
    ' "pending": ["what still has to be done"],\n'
    ' "files_changed": ["paths written or edited"],\n'
    ' "failing_tests_errors": ["failing tests and error messages, copied '
    'VERBATIM"],\n'
    ' "decisions": ["choices made, with the reason"],\n'
    ' "failed_approaches": ["what was tried and did not work, so it is not '
    'repeated"],\n'
    ' "next_step": "the single next action"}\n'
    "Use [] or \"\" for a field with nothing to say. Keep every entry terse. "
    "Name concrete files, symbols and commands. Never invent: leave a field "
    "empty rather than guess.")


def enabled() -> bool:
    return os.environ.get("AIFORGE_COMPACT_STRUCTURED", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _key(raw) -> str:
    k = re.sub(r"\(.*?\)", " ", str(raw).lower())
    k = re.sub(r"[^a-z0-9]+", "_", k).strip("_")
    k = _ALIASES.get(k, k)
    return k if k in FIELDS else ""


def _clip(text, limit: int) -> str:
    return " ".join(str(text).split())[:limit].strip()


def _as_list(value, limit: int) -> list:
    if value is None:
        return []
    bullet = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
    if isinstance(value, str):
        parts = [bullet.sub("", ln) for ln in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        parts = []
        for v in value:
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            parts.append("" if v is None else bullet.sub("", str(v)))
    else:
        parts = [str(value)]
    return [c for c in (_clip(p, limit) for p in parts) if c]


def _as_str(value) -> str:
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v is not None)
    return _clip(value if value is not None else "", _ITEM_CHARS)


def _normalise(raw: dict) -> dict:
    out = {k: ([] if kind == "list" else "") for k, kind in FIELDS.items()}
    for k, v in raw.items():
        field = _key(k)
        if not field:
            continue
        if FIELDS[field] == "str":
            out[field] = out[field] or _as_str(v)
        else:
            limit = _ERR_CHARS if field == "failing_tests_errors" else _ITEM_CHARS
            out[field] = (out[field] + _as_list(v, limit))[:_MAX_ITEMS if field != "files_changed" else _MAX_FILES]
    return out


def _json_object(text: str):
    body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(body[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


_LABEL_LINE = re.compile(r"^\s*(?:[-*#]+\s*)?\**([A-Za-z][A-Za-z /&_()-]{2,48}?)\**\s*:\s*(.*)$")


def _labelled(text: str):
    """The same record written as ``FIELD: value`` lines (a model that ignored
    the JSON instruction but kept the fields)."""
    raw: dict = {}
    cur = ""
    for line in text.splitlines():
        m = _LABEL_LINE.match(line)
        field = _key(m.group(1)) if m else ""
        if field:
            cur = field
            first = m.group(2).strip()
            raw[cur] = [first] if first else []
        elif cur and line.strip():
            raw[cur].append(line.strip())
    return raw or None


def valid(rec: dict) -> bool:
    """A record is usable when it names at least two fields, one of which says
    where the work stands (goal, done, pending or the next step)."""
    filled = [k for k in FIELDS if rec.get(k)]
    return len(filled) >= 2 and any(
        k in filled for k in ("goal", "done_and_verified", "pending", "next_step"))


def parse(text) -> "dict | None":
    """The typed record in a model reply, or None when it has none."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = _json_object(text)
    if raw is None:
        raw = _labelled(text)
    if not raw:
        return None
    rec = _normalise(raw)
    return rec if valid(rec) else None


def render(rec: dict) -> str:
    """The record as the labelled lines that go into the breadcrumb."""
    lines: list = []
    for field, kind in FIELDS.items():
        val = rec.get(field)
        if not val:
            continue
        label = _LABELS[field]
        if kind == "str":
            lines.append(f"{label}: {val}")
        elif field == "files_changed":
            lines.append(f"{label}: " + " · ".join(val))
        else:
            lines.append(f"{label}:")
            lines.extend(f"- {v}" for v in val)
    return "\n".join(lines)


def summarise(text) -> str:
    """What the background call stores: the rendered record, or "" when the
    reply holds none (the breadcrumb then stands). With the switch off the text
    passes through untouched."""
    if not enabled():
        return text if isinstance(text, str) else ""
    rec = parse(text)
    return render(rec) if rec else ""


__all__ = ["FIELDS", "SYSTEM", "enabled", "parse", "render", "summarise", "valid"]
