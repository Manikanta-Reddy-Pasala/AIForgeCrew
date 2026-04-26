"""Provider registry — auto-imports the bundled providers.

Drop a new file under this package and call
:func:`register_provider` to make it available to the router.
"""
from __future__ import annotations

from ..types import Provider

PROVIDERS: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    """Add a provider to the global registry. Idempotent on name."""
    PROVIDERS[provider.name] = provider


def get(name: str) -> Provider | None:
    return PROVIDERS.get(name)


# Auto-register bundled providers on package import. New providers
# added here are picked up by the router with no router edit.
from . import local as _local      # noqa: F401, E402
from . import gemini as _gemini    # noqa: F401, E402
from . import anthropic as _anthropic  # noqa: F401, E402
from . import openai as _openai    # noqa: F401, E402
from . import ollama_cloud as _ollama_cloud  # noqa: F401, E402

__all__ = ["PROVIDERS", "register_provider", "get"]
