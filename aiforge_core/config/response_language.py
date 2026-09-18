"""The language and English variety every model writes in, for people.

Two global choices (Settings → Response language): Indian or US English, and
a Simple or Formal tone. They reach EVERY model call at the two choke
points — ``llm.client.complete``/``complete_raw`` (chat, jobs, tickets, the
tools that write Jira, Confluence and email) and the ADK ``EscalatingLlm``
(team and ticket pipelines) — plus the chat system prompt, where it is placed
high so a tight window cannot trim it.

It governs PROSE only — code, identifiers, paths, commands, JSON keys, labels
and protocol markers are never "translated" to match it. Internal roles whose
output only machines and the memory store read (classifiers, triage, graders,
memory distillation, query rewriting) are left alone: their output is parsed
or searched, not read.

Codes come from a fixed catalogue, never free text: the directive is written
into system prompts, so a stored value must not be able to carry instructions
of its own.

Resolution: ``$AIFORGE_CONFIG_DIR/response_language.json`` (the UI writes
here), then ``AIFORGE_RESPONSE_LANGUAGE`` / ``AIFORGE_RESPONSE_STYLE``, then
"" = no preference (the model's
own default, the old behaviour).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()

_MARK = "RESPONSE LANGUAGE:"

_PROSE_ONLY = ("This governs prose only: never change code, identifiers, file "
               "paths, commands, tool names, JSON keys, enum or label values, "
               "protocol markers (THOUGHT:, ACTION:, FINAL:, ASK:), slugs, "
               "one-word verdicts you are told to reply with (CLEAN, PASS, "
               "FAIL, YES, NO…) or quoted text to match it.")

# Roles whose output is read by code or by the memory store, not by a person
# (substring match, like model_registry's role families).
_INTERNAL_ROLES = ("memory", "learner", "triage", "classif", "ctx_", "enhancer",
                   "refiner", "feedback", "live_verifier", "grader", "judge",
                   "gap_eval", "verif", "router", "embed")

# code -> (label shown in Settings, how the reply should be written)
_CATALOGUE: dict[str, tuple[str, str]] = {
    "en-IN": ("English (India)",
              "Indian English: British spelling (colour, organise, centre), "
              "DD-MM-YYYY dates, ₹ for rupees, and lakh/crore with Indian "
              "digit grouping (1,50,000) for large amounts"),
    "en-US": ("English (US)",
              "American English: American spelling (color, organize, center) "
              "and vocabulary, MM/DD/YYYY dates"),
}

# style -> (label shown in Settings, how the reply should read)
_STYLES: dict[str, tuple[str, str]] = {
    "simple": ("Simple",
               "Keep it simple: short sentences and plain everyday words; "
               "explain any technical term you cannot avoid."),
    "formal": ("Formal",
               "Use a formal, professional tone, as for business documents "
               "and client email."),
}


def _path() -> Path:
    return Path(os.path.expanduser(str(config_dir()))) / "response_language.json"


def _canon(code: str | None) -> str:
    """The catalogue's spelling of ``code`` ("en_in" → "en-IN"), or "" when it
    is not in the catalogue."""
    c = (code or "").strip().replace("_", "-").lower()
    return next((k for k in _CATALOGUE if k.lower() == c), "")


def _canon_style(style: str | None) -> str:
    s = (style or "").strip().lower()
    return s if s in _STYLES else ""


def options() -> list[dict]:
    """Every language, "" (no preference) first — the Settings dropdown."""
    return ([{"code": "", "label": "Model default (no preference)"}]
            + [{"code": k, "label": v[0]} for k, v in _CATALOGUE.items()])


def style_options() -> list[dict]:
    """Every style, "" (no preference) first."""
    return ([{"code": "", "label": "Model default"}]
            + [{"code": k, "label": v[0]} for k, v in _STYLES.items()])


def _stored() -> dict:
    p = _path()
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            return raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001 — a broken file is "not set"
            pass
    return {}


def get() -> str:
    """The chosen language code, or "" for no preference."""
    raw = _stored()
    if "language" in raw:                  # a stored "" means "no preference"
        return _canon(raw.get("language"))
    return _canon(os.environ.get("AIFORGE_RESPONSE_LANGUAGE"))


def get_style() -> str:
    """The chosen style ("simple" / "formal"), or "" for no preference."""
    raw = _stored()
    if "style" in raw:
        return _canon_style(raw.get("style"))
    return _canon_style(os.environ.get("AIFORGE_RESPONSE_STYLE"))


def _save(**changes) -> None:
    with _LOCK:
        data = {"language": get(), "style": get_style(), **changes}
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic.write_text(p, json.dumps(data, indent=2))


def set_language(code: str) -> str:
    """Store ``code`` ("" clears the preference). Unknown → ValueError."""
    canon = _canon(code)
    if (code or "").strip() and not canon:
        raise ValueError(f"unknown language: {code!r}")
    _save(language=canon)
    return canon


def set_style(style: str) -> str:
    """Store ``style`` ("" clears it). Unknown → ValueError."""
    canon = _canon_style(style)
    if (style or "").strip() and not canon:
        raise ValueError(f"unknown style: {style!r}")
    _save(style=canon)
    return canon


def directive(code: str | None = None, style: str | None = None) -> str:
    """The system-prompt paragraph for ``code`` and ``style`` (default: the
    stored choices), or "" when neither is set."""
    c = get() if code is None else _canon(code)
    st = get_style() if style is None else _canon_style(style)
    if not c and not st:
        return ""
    parts = [f"{_MARK} this applies to everything you produce for people — "
             "replies, questions, Jira issues and comments, Confluence pages, "
             "emails, PR/MR descriptions, reports and summaries."]
    if c:
        parts.append(f"Write it in {_CATALOGUE[c][1]}.")
    if st:
        parts.append(_STYLES[st][1])
    parts.append(_PROSE_ONLY)
    return " ".join(parts)


def for_role(role: str | None) -> str:
    """:func:`directive` for a model call made as ``role``; "" for the internal
    roles, whose output people never read."""
    r = (role or "").strip().lower()
    if any(k in r for k in _INTERNAL_ROLES):
        return ""
    return directive()


def _has_mark(content) -> bool:
    if isinstance(content, str):
        return _MARK in content
    if isinstance(content, list):          # multimodal parts
        return any(isinstance(p, dict) and _MARK in str(p.get("text") or "")
                   for p in content)
    return False


def apply(role: str | None, messages: list[dict]) -> list[dict]:
    """``messages`` with the directive added to the system message. A call
    that has NO system message keeps it that way — some callers (review_gates)
    send one plain user turn on purpose, because a local model returns nothing
    when a system message is present — so the directive leads the first user
    turn instead. Returns the SAME list when there is nothing to add or a
    system message already carries it; never mutates the caller's."""
    text = for_role(role)
    if not text or not messages:
        return messages
    # Only a SYSTEM message counts: a pasted page or a tool result that
    # happens to contain the marker must not switch the setting off.
    if any(isinstance(m, dict) and m.get("role") == "system"
           and _has_mark(m.get("content")) for m in messages):
        return messages
    out = list(messages)
    first = out[0] if isinstance(out[0], dict) else {}
    if first.get("role") == "system" and isinstance(first.get("content"), str):
        out[0] = {**first, "content": f"{first['content']}\n\n{text}"}
        return out
    for i, m in enumerate(out):
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                out[i] = {**m, "content": f"[{text}]\n\n{content}"}
            elif isinstance(content, list):
                out[i] = {**m, "content": [{"type": "text", "text": f"[{text}]"},
                                           *content]}
            return out
    return out


__all__ = ["apply", "directive", "for_role", "get", "get_style", "options",
           "set_language", "set_style", "style_options"]
