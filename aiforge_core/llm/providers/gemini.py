"""Google Gemini provider — OpenAI-compat endpoint via AI Studio.

Single API key (``AIFORGE_GOOGLE_API_KEY``) used for every role.
Model defaults to ``gemini-2.5-flash``; override per-role with
``AIFORGE_<ROLE>_GEMINI_MODEL`` (e.g. ``gemini-2.5-pro``).
"""
from __future__ import annotations

import os

from ..types import Endpoint
from . import register_provider


_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai"
)
_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider:
    name = "gemini"

    def is_available(self) -> bool:
        return bool(os.environ.get("AIFORGE_GOOGLE_API_KEY"))

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        model = (
            os.environ.get(f"AIFORGE_{role_up}_GEMINI_MODEL")
            or os.environ.get("AIFORGE_GEMINI_MODEL")
            or _DEFAULT_MODEL
        )
        return Endpoint(
            base_url=_GEMINI_BASE_URL,
            api_key=os.environ.get("AIFORGE_GOOGLE_API_KEY", ""),
            model=model,
            provider=self.name,
            role=role,
            extras={},
        )


register_provider(GeminiProvider())
