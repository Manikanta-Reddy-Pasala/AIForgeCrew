"""LLM backend compatibility shims.

Different OpenAI-compatible servers accept different ``response_format``
values:

- **OpenAI cloud** (api.openai.com): ``json_object``, ``json_schema``, ``text``
- **LM Studio MLX engine** (default :1234): ``json_schema``, ``text`` only —
  rejects ``json_object`` with HTTP 400 "must be 'json_schema' or 'text'"
- **vLLM / Ollama Cloud / SGLang**: usually accept ``json_object`` but
  some versions only accept ``json_schema``

This module returns a universally-acceptable ``response_format`` value
based on an env knob. Default = ``json_schema`` with a permissive
"object" schema, which every modern backend (OpenAI 2024+, LM Studio,
vLLM 0.4+) accepts.

Env knobs:
  AIFORGE_CODEMEM_RESPONSE_FORMAT=json_schema   (default; works everywhere)
  AIFORGE_CODEMEM_RESPONSE_FORMAT=json_object   (legacy OpenAI; breaks LM Studio)
  AIFORGE_CODEMEM_RESPONSE_FORMAT=text          (no JSON constraint; use prompt)
  AIFORGE_CODEMEM_RESPONSE_FORMAT=none          (drop the field entirely)

Why default ``json_schema``: it's the only mode that's BOTH constrained
(model returns parseable JSON) AND universally accepted. ``text`` works
but loses JSON enforcement, leading to occasional prose responses that
the caller's ``_parse`` has to retry. ``json_object`` loses LM Studio.
"""
from __future__ import annotations

import os


_PERMISSIVE_OBJECT_SCHEMA: dict = {
    "name": "structured_output",
    "schema": {
        "type": "object",
        "additionalProperties": True,
    },
    "strict": False,
}


def response_format() -> dict | None:
    """Return a backend-compatible ``response_format`` kwarg value, or
    ``None`` to drop the kwarg entirely. Caller pattern::

        kwargs = {"response_format": fmt} if (fmt := response_format()) else {}
        client.chat.completions.create(..., **kwargs)
    """
    mode = os.environ.get("AIFORGE_CODEMEM_RESPONSE_FORMAT", "json_schema").lower()
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "text":
        return {"type": "text"}
    if mode == "none":
        return None
    # default = json_schema (works on LM Studio + OpenAI + vLLM)
    return {"type": "json_schema", "json_schema": _PERMISSIVE_OBJECT_SCHEMA}


__all__ = ["response_format"]
