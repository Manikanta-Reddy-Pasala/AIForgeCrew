"""Reasoning OFF for quick, direct-output roles — the switch servers honour.

A fast role (triage classifier, enhancer, capture classify, title, …) wants a
plain answer. The ``/no_think`` soft switch and the ``enable_thinking: false``
chat-template kwarg are ignored by some model/server pairs: measured on LM
Studio with a Qwen reasoning model, a triage classify with ``max_tokens=8``
came back EMPTY (all 8 tokens spent reasoning) and was re-posted by the
empty-retry ladder, and a 250-token capture classify took 16s. The OpenAI
``reasoning_effort: "none"`` field is honoured there: 0 reasoning tokens, the
one-word answer in under a second.

Not every server knows the field (a non-reasoning cloud model may 400 on it).
The first 400 whose body names the reasoning parameter is remembered per
(base URL, model) — a gateway serving several models may reject it for one
and honour it for the others — and the request is re-sent without the field,
whoever put it there (a caller's own ``reasoning_effort`` would fail the same
way). Such a model pays one extra round trip, once. Any other 4xx that
merely contains the word "reasoning" is not remembered.

Env:
  AIFORGE_FAST_ROLE_REASONING_EFFORT  value sent for fast roles (default
                                      ``none``; empty / ``off`` disables)
"""
from __future__ import annotations

import os
import re
import threading
import urllib.error

_LOCK = threading.Lock()
#: (base_url, model, field) a server answered 400 for.
_REJECTED: set[tuple[str, str, str]] = set()
FIELD = "reasoning_effort"
#: The reasoning switches AIForge may add to an OpenAI-compatible request.
FIELDS = ("reasoning_effort", "thinking", "chat_template_kwargs")

_BAD = (r"(?:unrecognized|unsupported|unknown|invalid|unexpected|extra|"
        r"not\s+(?:supported|allowed|permitted|recogni[sz]ed))")

#: A 400 body that names the field as the problem.
_PATTERNS = {
    # unchanged from the single-field version
    "reasoning_effort": re.compile(
        r"reasoning_effort"
        r"|(?:unrecognized|unsupported|unknown|invalid|unexpected|extra)\s+"
        r"(?:request\s+)?(?:argument|parameter|param|field|input|key)s?"
        r"[^.\n]{0,40}\breasoning\b"
        r"|\breasoning\b[\w.\"']*\s+(?:is\s+)?(?:not\s+(?:supported|allowed|"
        r"permitted|recogni[sz]ed)|unsupported|unrecognized|unknown|invalid)"),
    # "Unrecognized request argument supplied: thinking",
    # pydantic {"type": "extra_forbidden", "loc": ["body", "thinking"]}
    "thinking": re.compile(
        _BAD + r"[^.\n]{0,60}[\"'\s:,\[]thinking\b(?!_)"
        r"|[\"']thinking[\"'][^.\n]{0,80}" + _BAD
        + r"|extra_forbidden[^\n]{0,80}[\"']thinking[\"']"),
    "chat_template_kwargs": re.compile(r"chat_template_kwargs"),
}


def _effort() -> str:
    v = os.environ.get("AIFORGE_FAST_ROLE_REASONING_EFFORT", "none").strip()
    return "" if v.lower() in ("", "off", "0", "false", "no") else v


def _key(base_url: str, model: str = "", field: str = FIELD) -> tuple[str, str, str]:
    return (base_url or "").rstrip("/").lower(), (model or "").strip(), field


def rejected(base_url: str, model: str = "", field: str = FIELD) -> bool:
    with _LOCK:
        return _key(base_url, model, field) in _REJECTED


def strip_rejected(body: dict, base_url: str, model: str = "") -> dict:
    """Drop from ``body`` (in place) every reasoning field this server refused."""
    for f in FIELDS:
        if f in body and rejected(base_url, model, f):
            body.pop(f, None)
    return body


def extras_for(base_url: str, fast_role: bool, model: str = "") -> dict:
    """The body fields to add for this call: ``{"reasoning_effort": …}`` for a
    fast role on a model that has not rejected it, else ``{}``."""
    if not fast_role:
        return {}
    eff = _effort()
    if not eff or rejected(base_url, model):
        return {}
    return {FIELD: eff}


def note_rejection(base_url: str, exc: Exception, model: str = "",
                   sent: "dict | None" = None) -> bool:
    """True (and remembered for this model) when ``exc`` is a 400 whose body
    names one of the reasoning fields — the caller then re-sends without it.
    With ``sent`` (the request body), only a field that was actually sent
    counts. Anything else: False, nothing recorded."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if int(getattr(exc, "code", 0) or 0) != 400:
        return False
    try:
        from .client._errors import http_err_body
        body = http_err_body(exc).lower()
    except Exception:  # noqa: BLE001
        body = ""
    hit = [f for f in FIELDS
           if _PATTERNS[f].search(body) and (sent is None or f in sent)]
    if not hit:
        return False
    with _LOCK:
        for f in hit:
            _REJECTED.add(_key(base_url, model, f))
    return True


def reset() -> None:
    """Forget remembered rejections (tests)."""
    with _LOCK:
        _REJECTED.clear()


__all__ = ["FIELD", "FIELDS", "extras_for", "note_rejection", "rejected", "reset",
           "strip_rejected"]
