"""OpenAI (or any OpenAI-compat) provider.

Set ``OPENAI_API_KEY`` to enable. Custom base via
``AIFORGE_OPENAI_BASE_URL`` (e.g. point at OpenRouter / Together /
DeepSeek / a self-hosted gateway). Model default: ``gpt-5-mini``,
override per-role with ``AIFORGE_<ROLE>_OPENAI_MODEL``.
"""
from __future__ import annotations

import os

from ..types import Endpoint
from . import register_provider


_DEFAULT_MODEL = "gpt-5-mini"


class OpenAIProvider:
    name = "openai"

    def is_available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        base_url = (
            os.environ.get("AIFORGE_OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = (
            os.environ.get(f"AIFORGE_{role_up}_OPENAI_MODEL")
            or os.environ.get("AIFORGE_OPENAI_MODEL")
            or _DEFAULT_MODEL
        )
        return Endpoint(
            base_url=base_url,
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            model=model,
            provider=self.name,
            role=role,
            extras={},
        )


register_provider(OpenAIProvider())
