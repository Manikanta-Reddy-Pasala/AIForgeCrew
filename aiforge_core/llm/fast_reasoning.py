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
The first rejection that names it is remembered per base URL and the request is
re-sent without it, so such a server pays one extra round trip, once.

Env:
  AIFORGE_FAST_ROLE_REASONING_EFFORT  value sent for fast roles (default
                                      ``none``; empty / ``off`` disables)
"""
from __future__ import annotations

import os
import threading
import urllib.error

_LOCK = threading.Lock()
_REJECTED: set[str] = set()


def _effort() -> str:
    v = os.environ.get("AIFORGE_FAST_ROLE_REASONING_EFFORT", "none").strip()
    return "" if v.lower() in ("", "off", "0", "false", "no") else v


def _key(base_url: str) -> str:
    return (base_url or "").rstrip("/").lower()


def extras_for(base_url: str, fast_role: bool) -> dict:
    """The body fields to add for this call: ``{"reasoning_effort": …}`` for a
    fast role on a server that has not rejected it, else ``{}``."""
    if not fast_role:
        return {}
    eff = _effort()
    if not eff:
        return {}
    with _LOCK:
        if _key(base_url) in _REJECTED:
            return {}
    return {"reasoning_effort": eff}


def note_rejection(base_url: str, exc: Exception) -> bool:
    """True (and remembered) when ``exc`` is a 4xx that names the field — the
    caller then re-sends without it. Anything else: False, nothing recorded."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if not (400 <= int(getattr(exc, "code", 0) or 0) < 500) or exc.code == 429:
        return False
    try:
        from .client._errors import _http_err_body
        body = _http_err_body(exc).lower()
    except Exception:  # noqa: BLE001
        body = ""
    if "reasoning" not in body:
        return False
    with _LOCK:
        _REJECTED.add(_key(base_url))
    return True


def reset() -> None:
    """Forget remembered rejections (tests)."""
    with _LOCK:
        _REJECTED.clear()


__all__ = ["extras_for", "note_rejection", "reset"]
