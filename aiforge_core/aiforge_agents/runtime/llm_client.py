"""Thin OpenAI-compat LLM client for archetype.run().

Used in P1 minimal loop. Will be replaced by ADK LlmAgent in P2.
"""
from __future__ import annotations

import json
import os
import re

from openai import OpenAI


def _client(base_url: str | None = None) -> OpenAI:
    return OpenAI(
        base_url=base_url or os.environ.get(
            "AIFORGE_AGENTS_LM_URL",
            os.environ.get("AIFORGE_CODEMEM_LM_URL", "http://127.0.0.1:1234/v1"),
        ),
        api_key=os.environ.get("AIFORGE_AGENTS_LM_KEY", "lm-studio"),
    )


_FENCE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def call_json(*, model: str, system: str, user: str,
              temperature: float = 0.0, max_tokens: int = 4000) -> dict | None:
    """Strict-JSON LLM call. Returns parsed dict, or None on parse fail."""
    client = _client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or ""
    cleaned = _FENCE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def call_text(*, model: str, system: str, user: str,
              temperature: float = 0.0, max_tokens: int = 4000) -> str:
    client = _client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""
