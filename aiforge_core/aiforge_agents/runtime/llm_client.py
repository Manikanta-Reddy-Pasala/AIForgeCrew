"""Thin LLM client for archetype.run().

This module is now a delegating shim over :mod:`aiforge_core.llm` —
archetypes get the full router + health probe + cloud auto-escalation
+ quality-aware fallback for free instead of bypassing them.

Two archetype-friendly entry points:

* :func:`call_text` — return raw assistant content as string.
* :func:`call_json` — strict JSON parse + fence stripping; ``None`` on
  parse failure (existing archetype contract).

Legacy ``model=...`` kwarg is preserved for backwards compatibility but
is now informational only — the canonical model comes from the router
configured for the named ``role``. Archetypes should pass ``role=`` to
benefit from the per-role provider routing.
"""
from __future__ import annotations

import json
import os
import re

from aiforge_core.llm import complete as _complete


_FENCE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _role_for(model_hint: str | None) -> str:
    """Map archetype-supplied model hint → router role.

    Archetypes historically passed a literal model id (e.g.
    ``Qwen3-Coder-Next``); the router uses logical role names. This
    function infers the role from the model id when an explicit
    ``AIFORGE_LLM_CLIENT_DEFAULT_ROLE`` is unset.
    """
    explicit = os.environ.get("AIFORGE_LLM_CLIENT_DEFAULT_ROLE")
    if explicit:
        return explicit
    if not model_hint:
        return "doer"
    m = model_hint.lower()
    if "coder" in m or "qwen3-coder" in m:
        return "doer"
    if "27b" in m or "32b" in m or "planner" in m:
        return "planner"
    return "doer"


def call_text(*, model: str | None = None,
              system: str, user: str,
              role: str | None = None,
              temperature: float = 0.0,
              max_tokens: int = 4000) -> str:
    """One-shot text completion. Routes via the pluggable LLM layer."""
    chosen_role = role or _role_for(model)
    return _complete(
        chosen_role,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )


def call_json(*, model: str | None = None,
              system: str, user: str,
              role: str | None = None,
              temperature: float = 0.0,
              max_tokens: int = 4000) -> dict | None:
    """Strict-JSON LLM call. Returns parsed dict, or ``None`` on parse fail.

    Sends ``response_format={"type": "json_object"}`` via ``extras`` so
    OpenAI-compat servers honour it; mlx-lm ignores it harmlessly. Also
    strips Markdown fences from models that ignore the directive.
    """
    chosen_role = role or _role_for(model)
    raw = _complete(
        chosen_role,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        extras={"response_format": {"type": "json_object"}},
    )
    cleaned = _FENCE.sub("", raw or "").strip()
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None
