"""Role → Provider router.

Resolution order (highest priority first):

1. ``AIFORGE_<ROLE>_PROVIDER`` env var. Per-role override; e.g.
   ``AIFORGE_DOER_PROVIDER=anthropic`` while everyone else stays
   on local. Useful when the doer benefits from a stronger model
   but the planner is fine on cheap local.
2. ``AIFORGE_PRIMARY_BACKEND`` env var. Single global default;
   set from the Settings UI.
3. ``AIFORGE_DOER_PRIMARY_BACKEND`` legacy alias.
4. Hardcoded default ``"local"``.

If the chosen provider isn't available (e.g. ``gemini`` selected
but no API key), the router silently falls through to ``"local"``
rather than crashing.

:func:`fallback` returns the OTHER side for retry chains. Picks
the first available provider that's neither the current primary
nor the role's per-role override.
"""
from __future__ import annotations

import os

from .types import Endpoint
from . import providers as _providers


def _global_default() -> str:
    return (
        os.environ.get("AIFORGE_PRIMARY_BACKEND")
        or os.environ.get("AIFORGE_DOER_PRIMARY_BACKEND")
        or "local"
    ).lower()


def resolve(role: str) -> Endpoint:
    """Pick + build the endpoint for ``role``."""
    name = (
        os.environ.get(f"AIFORGE_{role.upper()}_PROVIDER")
        or _global_default()
    ).lower()
    prov = _providers.get(name)
    if prov is None or not prov.is_available():
        prov = _providers.get("local")
    assert prov is not None  # local always registers + is_available
    return prov.endpoint(role)


def fallback(role: str) -> Endpoint | None:
    """Return the next-best Endpoint for ``role`` if the primary fails.

    Picks the first available provider whose name differs from the
    primary's. Returns None when no fallback is configured (only
    one provider available).
    """
    primary = resolve(role)
    for name, prov in _providers.PROVIDERS.items():
        if name == primary.provider:
            continue
        if prov.is_available():
            return prov.endpoint(role)
    return None


def list_providers() -> list[dict]:
    """Snapshot of the registry, for the Settings UI.

    Providers with ``hidden = True`` are filtered out unless
    ``AIFORGE_SHOW_<NAME>=1`` is set — keeps Gemini code in the
    registry (rate limiter, web_search) but off the primary-backend
    selector while ops standardise on Ollama Cloud.
    """
    out: list[dict] = []
    for name, prov in _providers.PROVIDERS.items():
        hidden = bool(getattr(prov, "hidden", False))
        if hidden and os.environ.get(
            f"AIFORGE_SHOW_{name.upper()}", ""
        ).strip().lower() not in ("1", "true", "yes"):
            continue
        out.append({"name": name, "available": prov.is_available()})
    return out
