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


# Strip code fences (3+ backticks). Local models occasionally emit
# 4 trailing backticks because the response format directive
# `response_format=json_object` collides with their training-set
# bias toward fenced output.
# Grouped explicitly: opening fence OR closing fence, not one alternation
# whose precedence a reader has to derive.
# POSSESSIVE quantifiers (`++`, Python 3.11+ and this project requires >=3.11).
# `\W+$`-style strips backtrack super-linearly on input that does NOT match:
# the engine retries the run at every length before giving up. `++` never
# gives characters back, which is exactly right for a strip and turns the
# scan linear.
_FENCE = re.compile(r"^`{3,}+(?:json)?\s*+\n?|\n?`{3,}+\s*+$", re.MULTILINE)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _loads_dict(text: str) -> "dict | None":
    """json.loads ``text`` and return it only when it parses to a dict."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _resilient_json_parse(raw: str | None) -> dict | None:
    """Tolerant JSON parse for local-model output: strip ``` fences, try strict,
    then the largest balanced {...} block, then a first-{ to last-} slice
    (catches nested fences). Returns the parsed dict, or None if all fail."""
    if not raw:
        return None
    cleaned = _FENCE.sub("", raw).strip()
    if not cleaned:
        return None
    obj = _loads_dict(cleaned)                          # 2. strict
    if obj is not None:
        return obj
    m = _JSON_OBJ_RE.search(cleaned)                    # 3. balanced object regex
    if m:
        obj = _loads_dict(m.group(0))
        if obj is not None:
            return obj
    if "{" in cleaned and "}" in cleaned:              # 4. first { to last }
        first, last = cleaned.find("{"), cleaned.rfind("}")
        if first != -1 and last > first:
            return _loads_dict(cleaned[first:last + 1])
    return None


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
              max_tokens: int = 4000,
              retry_on_invalid: bool = True) -> dict | None:
    """Strict-JSON LLM call with local-model resilience.

    Pipeline:
      1. Send with response_format=json_object (OpenAI honors, mlx-lm
         ignores harmlessly).
      2. Tolerant parse — strips 3+ backtick fences, falls back to
         balanced {...} extract, then to first-{ / last-} slice.
      3. On parse failure (and retry_on_invalid=True), retry ONCE with
         a stricter system prompt that re-states the format constraint
         and forbids prose. Tolerant parse again.
      4. Returns the parsed dict, or None if both attempts fail.

    Local models (Qwen3-Coder, mlx-lm) routinely emit:
      - 4+ trailing backticks
      - Stray prose around the JSON object
      - Markdown wrapper despite json_object directive
    The tolerant parser catches all three.
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
    parsed = _resilient_json_parse(raw)
    if parsed is not None:
        return parsed
    if not retry_on_invalid:
        return None
    # Retry once with a stricter format reminder. Lower temperature
    # to reduce creative drift on the retry.
    strict_system = (
        system
        + "\n\nIMPORTANT: respond with a SINGLE JSON object on its own "
        "line. No prose, no markdown fences, no explanation. The first "
        "non-whitespace character must be `{`."
    )
    raw2 = _complete(
        chosen_role,
        messages=[
            {"role": "system", "content": strict_system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=max_tokens,
        extras={"response_format": {"type": "json_object"}},
    )
    return _resilient_json_parse(raw2)
