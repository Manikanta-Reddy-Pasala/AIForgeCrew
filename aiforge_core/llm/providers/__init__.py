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
#
# ONE provider, deliberately (2026-09-03). The bundled `gemini` and `openai`
# providers were removed: each was a path that sent whole prompts to a vendor
# and needed nothing but an env var (AIFORGE_GOOGLE_API_KEY / OPENAI_API_KEY)
# to switch itself on — no UI step, no config file, no review. This is a
# local-first deployment where the operator chose the model endpoint precisely
# so that prompts stay on their own network, and a provider that self-activates
# from the environment quietly undoes that.
#
# Nothing is lost in reach: `openai_compatible` takes ANY base_url plus an
# optional key, which is how every one of those services is actually consumed
# (their own docs describe the OpenAI-compatible endpoint). The difference is
# that the operator has to TYPE the endpoint, so a cloud model is a decision
# rather than a side effect of an environment variable.
from . import openai_compatible as _openai_compatible  # noqa: F401, E402

__all__ = ["PROVIDERS", "register_provider", "get"]
