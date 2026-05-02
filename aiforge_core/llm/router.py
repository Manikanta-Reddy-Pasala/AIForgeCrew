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
    primary's AND passes the health probe (cache-checked). Returns None
    when no fallback is configured / all known providers are down.
    """
    from . import health as _health
    primary = resolve(role)
    for name, prov in _providers.PROVIDERS.items():
        if name == primary.provider:
            continue
        if not prov.is_available():
            continue
        if not _health.is_up(name, role=role):
            continue
        return prov.endpoint(role)
    return None


# Providers considered "cloud" for auto-escalation. Order = preference.
# A role currently on a non-cloud provider can be promoted to one of
# these when the request looks too big for local capacity.
_CLOUD_PROVIDERS: tuple[str, ...] = (
    "anthropic", "ollama_cloud", "openai", "gemini",
)

# Local context windows assumed when no role-specific cap configured.
# Used by ``escalate(role, est_tokens=...)`` to decide whether to flip
# off-local. Override per-role with ``AIFORGE_<ROLE>_CTX_WINDOW``.
_LOCAL_CTX_DEFAULT: int = 32_000


def _local_ctx_window(role: str) -> int:
    import os
    raw = os.environ.get(f"AIFORGE_{role.upper()}_CTX_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    raw = os.environ.get("AIFORGE_LOCAL_CTX_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _LOCAL_CTX_DEFAULT


def escalate(role: str, *, reason: str = "context_overflow",
             est_tokens: int | None = None,
             threshold: float = 0.8) -> Endpoint | None:
    """Auto-flip ``role`` to a cloud provider when local won't fit.

    Triggers (any of):
      - reason == 'context_overflow' AND est_tokens > local_ctx * threshold
      - reason in {'quality', 'timeout', 'breaker_close'}

    Returns the chosen cloud Endpoint, or ``None`` when no cloud
    provider is configured/available (caller stays on primary).

    Honoured envs:
      - ``AIFORGE_ESCALATE_DISABLE=1``  hard-disable escalation
      - ``AIFORGE_<ROLE>_CLOUD_PROVIDER=anthropic`` pin cloud target
      - ``AIFORGE_CLOUD_PROVIDER=...``  global cloud preference

    Decision is intentionally side-effect-free — caller (client.py)
    should log it. The router does not mutate process state.
    """
    import os
    if os.environ.get("AIFORGE_ESCALATE_DISABLE", "0") in ("1", "true"):
        return None
    primary = resolve(role)
    # Already on cloud — nothing to do.
    if primary.provider in _CLOUD_PROVIDERS:
        return None

    if reason == "context_overflow":
        if est_tokens is None:
            return None
        if est_tokens <= int(_local_ctx_window(role) * threshold):
            return None

    # Pick cloud target: per-role override > global > first available.
    pinned = (
        os.environ.get(f"AIFORGE_{role.upper()}_CLOUD_PROVIDER")
        or os.environ.get("AIFORGE_CLOUD_PROVIDER")
    )
    candidates: list[str] = []
    if pinned:
        candidates.append(pinned.lower())
    candidates.extend(_CLOUD_PROVIDERS)

    from . import health as _health
    for name in candidates:
        prov = _providers.get(name)
        if prov is None or not prov.is_available():
            continue
        if not _health.is_up(name, role=role):
            continue
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
