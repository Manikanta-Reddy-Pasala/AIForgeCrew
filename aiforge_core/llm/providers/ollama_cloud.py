"""Ollama Cloud provider — OpenAI-compat endpoint hosted by Ollama.

Single API key (``OLLAMA_CLOUD_API_KEY``) used for every role. Model
defaults to ``llama3.1:70b``; override per-role with
``AIFORGE_<ROLE>_OLLAMA_CLOUD_MODEL`` (e.g. ``qwen2.5:72b``).
"""
from __future__ import annotations

import os

from ..types import Endpoint
from . import register_provider


_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
_DEFAULT_MODEL = "llama3.1:70b"


class OllamaCloudProvider:
    name = "ollama_cloud"
    hidden = False

    def is_available(self) -> bool:
        return bool(os.environ.get("OLLAMA_CLOUD_API_KEY"))

    def rate_limits(self) -> dict | None:
        # Ollama Cloud does not publish hard caps — leave unset and let
        # the operator pin via AIFORGE_OLLAMA_CLOUD_RPM/_TPM env if a
        # paid plan caps RPM.
        return None

    def endpoint(self, role: str) -> Endpoint:
        role_up = role.upper()
        model = (
            os.environ.get(f"AIFORGE_{role_up}_OLLAMA_CLOUD_MODEL")
            or os.environ.get("AIFORGE_OLLAMA_CLOUD_MODEL")
            or _DEFAULT_MODEL
        )
        return Endpoint(
            base_url=_OLLAMA_CLOUD_BASE_URL,
            api_key=os.environ.get("OLLAMA_CLOUD_API_KEY", ""),
            model=model,
            provider=self.name,
            role=role,
            extras={},
        )


register_provider(OllamaCloudProvider())
