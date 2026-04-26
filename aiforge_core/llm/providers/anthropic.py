"""Anthropic (Claude) provider — OpenAI-compat endpoint when available.

Anthropic now ships an OpenAI-compatible chat-completions wrapper at
``https://api.anthropic.com/v1``. Set ``ANTHROPIC_API_KEY`` to enable.
Model default: ``claude-sonnet-4-5-20250929``; override per-role with
``AIFORGE_<ROLE>_ANTHROPIC_MODEL`` or globally with
``AIFORGE_ANTHROPIC_MODEL``.

Stub today — paid usage gated behind explicit role assignment via
``AIFORGE_<ROLE>_PROVIDER=anthropic`` so it's never auto-selected.
"""
from __future__ import annotations

import os

from ..types import Endpoint
from . import register_provider


_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


class AnthropicProvider:
    name = "anthropic"

    def is_available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def rate_limits(self) -> dict | None:
        # Anthropic paid tier — generous defaults; tighten via
        # AIFORGE_ANTHROPIC_RPM / _TPM env if your tier is lower.
        return {"rpm": 50, "tpm": 100_000}

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        model = (
            os.environ.get(f"AIFORGE_{role_up}_ANTHROPIC_MODEL")
            or os.environ.get("AIFORGE_ANTHROPIC_MODEL")
            or _DEFAULT_MODEL
        )
        return Endpoint(
            base_url=_BASE_URL,
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=model,
            provider=self.name,
            role=role,
            extras={},
        )


register_provider(AnthropicProvider())
