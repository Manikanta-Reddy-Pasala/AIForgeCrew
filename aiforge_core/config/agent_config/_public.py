"""Public listing / profile helpers for ``agent_config`` (split submodule)."""
from __future__ import annotations

from typing import Any

from ._state import _ARCHETYPES, _enriched_catalog, PROVIDERS


def archetypes() -> list[str]:
    """The public archetype roles. Legacy aliases stay invisible."""
    return list(_ARCHETYPES)


def list_providers() -> list[dict[str, Any]]:
    """Public providers in display order."""
    return [
        {
            "id": pid,
            "label": prov["label"],
            "default_model": prov["default_model"],
        }
        for pid, prov in PROVIDERS.items()
    ]


def list_models(provider: str) -> list[dict[str, Any]]:
    """Catalog for one provider — static curated + dynamic discovery."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    return _enriched_catalog(provider)


# ────────────────────────── Profile presets ────────────────────────────
# Profiles used to bulk-switch every archetype between provider stacks
# (local / Ollama Cloud). With ``openai_compatible`` the only provider
# there is nothing meaningful to preset, so the dict is empty — the "Apply
# to all" widget (per-role bulk set) covers the same need. Kept as an empty
# dict so callers (profiles() / apply_profile() / the API) don't break.

PROFILES: dict[str, dict[str, str]] = {}


def profiles() -> list[str]:
    """Names of the bundled profiles (none — see PROFILES)."""
    return list(PROFILES.keys())


def apply_profile(name: str) -> dict[str, dict[str, Any]]:
    """Bulk-assign one (provider, model) pair to every archetype.

    No profiles are bundled anymore (``openai_compatible`` is the only
    provider). Always raises ``ValueError`` so the API surfaces a clean
    404 rather than crashing.
    """
    raise ValueError(
        f"unknown profile: {name!r}. no profiles are bundled — use "
        f"'Apply to all' on the Home page instead."
    )
