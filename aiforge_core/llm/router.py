"""Role → Provider router.

Resolution order (highest priority first):

1. ``AIFORGE_<ROLE>_PROVIDER`` env var — operator final-say override.
2. ``agent_config.json`` per-role entry (Settings UI persists here).
   Allows the UI to flip e.g. Planner to Ollama Cloud while everything
   else stays local.
3. ``AIFORGE_PRIMARY_BACKEND`` env var (legacy global default).
4. Hardcoded default ``"openai_compatible"``.

If the chosen provider isn't available, the router silently falls
through to ``"openai_compatible"`` rather than crashing. The chosen
``model`` overrides whatever the provider would default to — so
per-archetype model pins also apply.

:func:`fallback` returns the OTHER side for retry chains.
"""
from __future__ import annotations

import os

from .types import Endpoint
from . import providers as _providers


def _global_default() -> str:
    return (
        os.environ.get("AIFORGE_PRIMARY_BACKEND")
        or os.environ.get("AIFORGE_DOER_PRIMARY_BACKEND")
        or "openai_compatible"
    ).lower()


def _agent_config_for(role: str) -> dict | None:
    """Pull provider+model from agent_config.json for the given role.

    The Settings UI writes here; without this hook the UI's selections
    never reached the router. Best-effort: any IO error returns None
    so the env var + global default still apply.
    """
    try:
        # NB: module lives at aiforge_core.config.agent_config — the old
        # aiforge_core.runtime.agent_config path never existed, so this
        # silently ImportError'd and the chat/client path fell back to
        # `local` for every role regardless of the UI's selection.
        from aiforge_core.config import agent_config as _acfg
        full = _acfg.load_all()
    except Exception:
        return None
    row = full.get(role)
    if not isinstance(row, dict):
        return None
    if not row.get("provider"):
        return None
    return row


def is_local_endpoint(role: str = "doer") -> bool:
    """True when ``role`` resolves to a loopback (local) OpenAI-compatible
    server — mlx-lm / ollama / llama.cpp / vLLM / LM Studio on 127.0.0.1 or
    localhost. Used to make serial-serving assumptions (e.g. don't fan out
    parallel calls to a single-model local box). Soft-fails to False."""
    try:
        base = (getattr(resolve(role), "base_url", "") or "").lower()
        return ("127.0.0.1" in base) or ("localhost" in base)
    except Exception:  # noqa: BLE001
        return False


def resolve(role: str) -> Endpoint:
    """Pick + build the endpoint for ``role``."""
    cfg = _agent_config_for(role)
    name = (
        os.environ.get(f"AIFORGE_{role.upper()}_PROVIDER")
        or (cfg.get("provider") if cfg else None)
        or _global_default()
    ).lower()
    prov = _providers.get(name)
    if prov is None or not prov.is_available():
        prov = _providers.get("openai_compatible")
    assert prov is not None  # openai_compatible always registers + is_available
    ep = prov.endpoint(role)
    # Override model if the agent_config entry pinned one.
    if cfg and cfg.get("model") and ep is not None:
        ep = Endpoint(
            base_url=ep.base_url, api_key=ep.api_key,
            model=cfg["model"], provider=ep.provider,
            role=ep.role, extras=ep.extras,
        )
    return ep


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


# Providers considered "cloud" for auto-escalation. Empty now that
# ``openai_compatible`` is the only provider — :func:`escalate` no-ops
# gracefully (returns None) so the caller stays on the primary endpoint.
# Keep in sync with agent_config._CLOUD_PROVIDERS_ORDERED.
_CLOUD_PROVIDERS: tuple[str, ...] = ()

# Local context windows assumed when no role-specific cap configured.
# Used by ``escalate(role, est_tokens=...)`` to decide whether to flip
# off-local. Override per-role with ``AIFORGE_<ROLE>_CTX_WINDOW``.
_LOCAL_CTX_DEFAULT: int = 32_000


def _local_ctx_window(role: str) -> int:
    import os
    # Per-role env override still wins (lets one role run a smaller window).
    raw = os.environ.get(f"AIFORGE_{role.upper()}_CTX_WINDOW")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # Global: operator-chosen value (UI → runtime_settings.json → env →
    # default). runtime_settings also reads AIFORGE_LOCAL_CTX_WINDOW for
    # back-compat.
    try:
        from aiforge_core.config import runtime_settings as _rs
        return _rs.get("context_window")
    except Exception:  # noqa: BLE001
        return _LOCAL_CTX_DEFAULT


def _overflow_needs_escalation(role: str, est_tokens: "int | None",
                              threshold: float) -> bool:
    """Whether a context_overflow reason warrants escalation: the estimate must
    exist AND exceed ``local_ctx * threshold``."""
    if est_tokens is None:
        return False
    return est_tokens > int(_local_ctx_window(role) * threshold)


def _first_available_cloud(role: str, candidates: list) -> "Endpoint | None":
    """The first candidate provider that is configured, available and healthy."""
    from . import health as _health
    for name in candidates:
        prov = _providers.get(name)
        if prov is None or not prov.is_available():
            continue
        if not _health.is_up(name, role=role):
            continue
        return prov.endpoint(role)
    return None


def escalate(role: str, *, reason: str = "context_overflow",
             est_tokens: int | None = None,
             threshold: float = 0.8) -> Endpoint | None:
    """Auto-flip ``role`` to a cloud provider when local won't fit.

    Triggers (any of): reason == 'context_overflow' AND est_tokens >
    local_ctx * threshold; reason in {'quality', 'timeout', 'breaker_close'}.
    Returns the chosen cloud Endpoint, or None when no cloud provider is
    configured/available. Side-effect-free — caller (client.py) logs it.

    Honoured envs: ``AIFORGE_ESCALATE_DISABLE=1``,
    ``AIFORGE_<ROLE>_CLOUD_PROVIDER``, ``AIFORGE_CLOUD_PROVIDER``.
    """
    import os
    if os.environ.get("AIFORGE_ESCALATE_DISABLE", "0") in ("1", "true"):
        return None
    if resolve(role).provider in _CLOUD_PROVIDERS:
        return None                       # already on cloud — nothing to do
    if reason == "context_overflow" and not _overflow_needs_escalation(
            role, est_tokens, threshold):
        return None
    # Pick cloud target: per-role override > global > first available.
    pinned = (os.environ.get(f"AIFORGE_{role.upper()}_CLOUD_PROVIDER")
              or os.environ.get("AIFORGE_CLOUD_PROVIDER"))
    candidates: list[str] = [pinned.lower()] if pinned else []
    candidates.extend(_CLOUD_PROVIDERS)
    return _first_available_cloud(role, candidates)


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
