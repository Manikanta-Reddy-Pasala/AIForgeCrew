"""Shared LLM endpoint picker for all agents.

Single env flag ``AIFORGE_PRIMARY_BACKEND`` controls which backend
is the *primary* across the entire pipeline:

- ``local`` (default): mlx-lm on the per-role port (planner :1235,
  rest :1234). Gemini-Flash is the fallback when the primary
  errors.
- ``gemini``: Google Gemini-Flash via the OpenAI-compat endpoint
  for all agents. mlx-lm is the fallback.

Per-role agents call :func:`pick` to get the right ``(base_url,
api_key, model)`` triple for their LM HTTP call. Doer's GA path
gets a richer dict via ga_tools.llm_config that wraps this.
"""
from __future__ import annotations

import os
from typing import NamedTuple


class LLMEndpoint(NamedTuple):
    base_url: str   # full /v1 base, ready for /chat/completions
    api_key: str
    model: str
    backend: str    # 'local' or 'gemini' — used for telemetry


_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_MODEL = "gemini-2.5-flash"


def _gemini_endpoint() -> LLMEndpoint | None:
    api_key = os.environ.get("AIFORGE_GOOGLE_API_KEY", "")
    if not api_key:
        return None
    return LLMEndpoint(
        base_url=_GEMINI_BASE_URL,
        api_key=api_key,
        model=_GEMINI_MODEL,
        backend="gemini",
    )


def _local_endpoint(role: str) -> LLMEndpoint:
    """Per-role local mlx-lm endpoint. Falls back to global LM_STUDIO_BASE_URL.

    Roles map to env keys: AIFORGE_<ROLE>_BASE_URL / _MODEL / _API_KEY.
    Planner runs on :1235, every other agent on :1234 in our deployment.
    """
    role_up = role.upper()
    base_url = (
        os.environ.get(f"AIFORGE_{role_up}_BASE_URL")
        or os.environ.get("AIFORGE_LM_BASE_URL")
        or "http://127.0.0.1:1234/v1"
    )
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    return LLMEndpoint(
        base_url=base_url,
        api_key=(
            os.environ.get(f"AIFORGE_{role_up}_API_KEY")
            or os.environ.get("LM_STUDIO_API_KEY")
            or "sk-local"
        ),
        model=(
            os.environ.get(f"AIFORGE_{role_up}_MODEL")
            or "mlx-local"
        ),
        backend="local",
    )


def pick(role: str) -> LLMEndpoint:
    """Return the endpoint the given role should hit.

    Honours AIFORGE_PRIMARY_BACKEND (legacy alias:
    AIFORGE_DOER_PRIMARY_BACKEND) so a Settings-page flip retags
    every role at once. Cloud falls back to local when key missing.
    """
    backend = (
        os.environ.get("AIFORGE_PRIMARY_BACKEND")
        or os.environ.get("AIFORGE_DOER_PRIMARY_BACKEND")
        or "local"
    ).lower()
    if backend == "gemini":
        cloud = _gemini_endpoint()
        if cloud is not None:
            return cloud
        # Key missing — silent-fallback to local rather than crash.
    return _local_endpoint(role)


def fallback(role: str) -> LLMEndpoint | None:
    """The OTHER backend, for retry-on-error chains.

    When primary is local, fallback is gemini (when key present).
    When primary is gemini, fallback is local mlx-lm.
    """
    primary = pick(role)
    if primary.backend == "local":
        return _gemini_endpoint()
    return _local_endpoint(role)
