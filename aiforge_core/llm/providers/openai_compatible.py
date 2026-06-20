"""Generic OpenAI-compatible provider — the deploy-anywhere endpoint.

Reads ``base_url`` + optional ``api_key`` + ``model`` from the per-role
``agent_config`` (set on the home page), with env vars overriding. One
provider covers LM Studio, OpenRouter, Groq, Together, vLLM, and any
cloud OpenAI-compat endpoint. Blank key = no token (OSS endpoints).

Resolution (highest first):
- base_url:  ``AIFORGE_<ROLE>_BASE_URL`` → ``AIFORGE_OPENAI_COMPAT_BASE_URL``
             → agent_config row base_url → ``http://127.0.0.1:1234/v1``
- api_key:   ``AIFORGE_OPENAI_COMPAT_API_KEY`` → ``AIFORGE_<ROLE>_API_KEY``
             → agent_config row api_key → ``"not-needed"``
- model:     ``AIFORGE_<ROLE>_MODEL`` → agent_config row model → ``"default"``
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..types import Endpoint
from . import register_provider

_DEFAULT_BASE = "http://127.0.0.1:1234/v1"
_NO_TOKEN = "not-needed"


def _ensure_v1(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


def _config_row(role: str) -> dict:
    try:
        from aiforge_core.config import agent_config as _acfg
        return _acfg.get(role) or {}
    except Exception:
        return {}


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def is_available(self) -> bool:
        # Always available; connection errors propagate to the caller.
        return True

    def rate_limits(self) -> dict | None:
        return None

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        row = _config_row(role)
        base_url = (
            os.environ.get(f"AIFORGE_{role_up}_BASE_URL")
            or os.environ.get("AIFORGE_OPENAI_COMPAT_BASE_URL")
            or row.get("base_url")
            or _DEFAULT_BASE
        )
        base_url = _ensure_v1(base_url)
        api_key = (
            os.environ.get("AIFORGE_OPENAI_COMPAT_API_KEY")
            or os.environ.get(f"AIFORGE_{role_up}_API_KEY")
            or row.get("api_key")
            or _NO_TOKEN
        )
        model = (
            os.environ.get(f"AIFORGE_{role_up}_MODEL")
            or row.get("model")
            or "default"
        )
        return Endpoint(
            base_url=base_url, api_key=api_key, model=model,
            provider=self.name, role=role, extras={},
        )


def probe(base_url: str, api_key: str | None = None,
          timeout: float = 6.0) -> dict:
    """Test-connection helper for the home page. GETs ``{base}/models``
    and returns ``{ok, models: [ids], error?}``. Never raises."""
    if not base_url or not base_url.strip():
        return {"ok": False, "error": "base_url required", "models": []}
    url = _ensure_v1(base_url.strip()) + "/models"
    headers = {"Accept": "application/json"}
    if api_key and api_key.strip() and api_key.strip() != _NO_TOKEN:
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "models": []}
    data = payload.get("data") if isinstance(payload, dict) else None
    models = []
    if isinstance(data, list):
        models = [m.get("id") for m in data
                  if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "models": models}


register_provider(OpenAICompatibleProvider())
