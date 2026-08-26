"""Config load / merge / resolution for ``agent_config`` (split submodule)."""
from __future__ import annotations

import logging
import os
from typing import Any

from ._state import (
    _DEFAULT_KEY,
    _ROLES,
    _fc,
    _local_default_model,
    _path,
)


def _env_default_row():
    """The AIFORGE_DEFAULT_* env one-endpoint default, or None when unset."""
    prov = os.environ.get("AIFORGE_DEFAULT_PROVIDER")
    if prov:
        return {
            "provider": prov,
            "model": os.environ.get("AIFORGE_DEFAULT_MODEL", ""),
            "base_url": os.environ.get("AIFORGE_DEFAULT_BASE_URL"),
            "api_key": os.environ.get("AIFORGE_DEFAULT_API_KEY"),
            "insecure_tls": os.environ.get(
                "AIFORGE_DEFAULT_INSECURE_TLS", "").strip().lower()
                in ("1", "true", "yes", "on"),
        }
    return None


def _borrow_configured_row(disk):
    """Last resort: borrow a CONFIGURED role's endpoint (preferred order, then
    any non-underscore role) so an unconfigured internal role gets a REAL model
    instead of the placeholder. None when nothing is configured."""
    # LAST RESORT: no explicit _default and no env → borrow a CONFIGURED role's
    # endpoint so an unconfigured internal role (e.g. `validator`, which isn't a
    # UI-configurable archetype) inherits a REAL local model instead of the
    # `local-model-unconfigured` placeholder — which points litellm at OpenAI's
    # default and 401s ("Incorrect API key: not-needed"), killing the team flow.
    for pref in ("doer", "chat", "verifier", "planner", "architect"):
        r = disk.get(pref)
        if isinstance(r, dict) and r.get("provider") and r.get("model"):
            return r
    for k, r in disk.items():
        if (not k.startswith("_") and isinstance(r, dict)
                and r.get("provider") and r.get("model")):
            return r
    return None


def _global_default_row() -> dict[str, Any] | None:
    """The operator's one-endpoint default for EVERY role.

    The pipeline has ~16 roles (the 6 archetypes plus triage / researcher /
    refiner / ctx_* / verify_* / gap_eval / chat). Configuring each by hand
    is a footgun — an unconfigured role silently falls back to ``local`` and
    breaks the whole team flow. A single ``_default`` entry (written by the
    home page's "Apply to all", or via ``AIFORGE_DEFAULT_*`` env) is
    inherited by any role without an explicit per-role override.

    Priority: persisted ``_default`` row > ``AIFORGE_DEFAULT_*`` env > none.
    """
    p = _path()
    disk: dict[str, Any] = {}
    if p.exists():
        try:
            disk = _fc.read_json(p) or {}
            d = disk.get(_DEFAULT_KEY)
            if isinstance(d, dict) and d.get("provider"):
                return d
        except Exception:  # noqa: BLE001
            disk = {}
    env_row = _env_default_row()
    if env_row is not None:
        return env_row
    return _borrow_configured_row(disk)


def _defaults() -> dict[str, dict[str, Any]]:
    """Per-role defaults. When a global ``_default`` is set, EVERY role
    inherits it (provider/model/base_url/api_key/insecure_tls); otherwise
    fall back to ``local`` with the dynamically-resolved local model id."""
    gd = _global_default_row()
    if gd and gd.get("provider"):
        model = (gd.get("model") or "").strip() or _local_default_model()
        return {
            role: {
                "provider": gd["provider"],
                "model": model,
                "base_url": gd.get("base_url"),
                "api_key": gd.get("api_key"),
                "insecure_tls": bool(gd.get("insecure_tls")),
            }
            for role in _ROLES
        }
    model = _local_default_model()
    return {
        role: {"provider": "openai_compatible", "model": model,
               "base_url": None}
        for role in _ROLES
    }


def _host_of(url: "str | None") -> str:
    try:
        import urllib.parse as _up
        return (_up.urlsplit(url or "").hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_bare_local(row: dict) -> bool:
    """A leftover "bare local" row — provider=local, no base_url, no api_key.

    Left behind by an old profile-apply or auto-discovery. A configured
    NON-LOCAL global default (cloud / internal endpoint) must win over one,
    or you set your endpoint (e.g. https://chat.ai.internal/...) and triage
    keeps hitting 127.0.0.1:1234 on the old default model.
    """
    return ((row.get("provider") or "local") == "local"
            and not row.get("base_url") and not row.get("api_key"))


def _registered_endpoint(row: dict, seed: dict) -> dict | None:
    """Where the model in ``row`` is REGISTERED to live, or None.

    Each model is registered with its own base_url, so letting a row inherit
    the global seed's endpoint sends a model that belongs to a different server
    to a host that never served it. Only a row with no URL of its own asks this,
    and ``connection_for`` answers None unless the registry names exactly one
    endpoint for the model — so an env-pinned or unregistered model keeps the
    seed fallback it always had. Soft-fail: resolution must never break a call.
    """
    try:
        from aiforge_core.config import model_registry as _mr
        return _mr.connection_for(row.get("model") or seed.get("model") or "")
    except Exception:  # noqa: BLE001
        return None


def _tls_of(row: dict, fallback: bool) -> bool:
    """An EXPLICIT per-role insecure_tls wins — including a deliberate ``false``
    that keeps strict TLS. Only a row that omits it inherits ``fallback``."""
    return (bool(row["insecure_tls"]) if row.get("insecure_tls") is not None
            else bool(fallback))


def _row_on_own_endpoint(seed: dict, row: dict, provider: str,
                         own: dict) -> dict:
    """The row resolved onto the endpoint its MODEL is registered with."""
    return {
        "provider": provider,
        "model": row.get("model") or seed["model"],
        "base_url": own["base_url"],
        "api_key": row.get("api_key") or own.get("api_key"),
        "insecure_tls": _tls_of(row, bool(own.get("insecure_tls"))),
    }


def _row_inheriting_seed(seed: dict, row: dict, provider: str,
                         row_base: str | None) -> dict:
    """The row with the seed's connection filled in behind it.

    Only inherit the seed's key when the row points at the SAME host (a
    different base_url is a different trust domain — don't leak the global
    cloud token to it). Since openai_compatible is the only provider,
    same_provider is always True, so the host check is what actually gates it.
    Compare HOSTNAMES (not the raw URL) so a trailing slash / case /
    explicit-port difference for the same endpoint doesn't wrongly drop it.
    """
    same_provider = provider == seed["provider"]
    same_host = (not row_base) or (_host_of(row_base)
                                   == _host_of(seed.get("base_url")))
    inherit_key = same_provider and same_host
    return {
        "provider": provider,
        "model": row.get("model") or seed["model"],
        "base_url": row_base or (seed.get("base_url") if same_provider else None),
        "api_key": row.get("api_key") or (
            seed.get("api_key") if inherit_key else None),
        "insecure_tls": _tls_of(
            row, same_provider and bool(seed.get("insecure_tls"))),
    }


def _merged_row(seed: dict, row: dict) -> dict:
    """``seed`` (the global-default row) overlaid with a per-role ``row``.

    A row that OMITS base_url / api_key / insecure_tls INHERITS them from the
    seed rather than nulling them. Without this, applying a profile (or a
    per-role Save) writes rows with base_url=None, which then shadow the
    operator's global endpoint and silently send every role back to
    http://127.0.0.1:1234 — the "I set one URL but it probes localhost" bug.
    An explicit per-role base_url still wins (lets us run mlx-lm on per-role
    ports).

    Before the seed gets a say, though, the model REGISTRY does: a row with no
    URL whose model is registered against a specific endpoint resolves THERE,
    so a model added from a second server is never called on the first one's
    host.
    """
    provider = row.get("provider") or seed["provider"]
    row_base = row.get("base_url")
    own = _registered_endpoint(row, seed) if not row_base else None
    if own:
        return _row_on_own_endpoint(seed, row, provider, own)
    return _row_inheriting_seed(seed, row, provider, row_base)


def _apply_disk_rows(cfg: dict, disk: dict, gd: dict | None) -> None:
    non_local_default = bool(gd and gd.get("provider")
                             and gd["provider"] != "local")
    for role, row in disk.items():
        if role not in _ROLES or not isinstance(row, dict):
            continue
        if non_local_default and _is_bare_local(row):
            continue                  # cfg[role] already = global default
        cfg[role] = _merged_row(cfg[role], row)


def _apply_env_overrides(cfg: dict) -> None:
    """AIFORGE_<ROLE>_MODEL / _PROVIDER / _BASE_URL / _API_KEY. Always wins
    over persisted JSON — the ops escape hatch."""
    fields = {"model": "MODEL", "provider": "PROVIDER",
              "base_url": "BASE_URL", "api_key": "API_KEY"}
    for role in _ROLES:
        cfg[role].setdefault("api_key", None)
        for key, suffix in fields.items():
            value = os.environ.get(f"AIFORGE_{role.upper()}_{suffix}")
            if value:
                cfg[role][key] = value


def load_all() -> dict[str, dict[str, Any]]:
    """Read the full per-role map, merging defaults for missing keys."""
    cfg: dict[str, dict[str, Any]] = {k: dict(v)
                                      for k, v in _defaults().items()}
    p = _path()
    if p.exists():
        try:
            disk = _fc.read_json(p)
            if isinstance(disk, dict):
                _apply_disk_rows(cfg, disk, _global_default_row())
        except Exception as exc:  # noqa: BLE001
            # Corrupt / truncated agent_config.json → fall back to defaults,
            # but say so once (silent fallback made "my config vanished"
            # impossible to diagnose).
            logging.getLogger("aiforge.agent_config").warning(
                "agent_config.json unreadable (%s) — using defaults; "
                "fix or reset the file (run.sh --reset-config).", exc)
    _apply_env_overrides(cfg)
    return cfg


def get(role: str) -> dict[str, Any]:
    """Return resolved config for one role: ``{provider, model, base_url}``."""
    if role == _DEFAULT_KEY:
        return _global_default_row() or {}
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    return load_all()[role]


def _row_for(role: str) -> dict[str, Any]:
    """Like :func:`get`, but unknown roles (the ``enhancer`` /
    ``validator`` stages, not in the configurable archetype list) resolve
    to the global ``_default`` instead of raising — so they run on the
    operator's configured model. ``get`` stays strict for callers (e.g.
    observability) that depend on the raise.
    """
    if role in _ROLES:
        return get(role)
    gd = _global_default_row()
    if gd and gd.get("provider"):
        model = (gd.get("model") or "").strip() or _local_default_model()
        return {"provider": gd["provider"], "model": model,
                "base_url": gd.get("base_url"), "api_key": gd.get("api_key"),
                "insecure_tls": bool(gd.get("insecure_tls"))}
    return {"provider": "openai_compatible", "model": _local_default_model(),
            "base_url": None, "api_key": None, "insecure_tls": False}


# Cheap-tier roles — throwaway ops (triage, enhancer, titling) that should run
# on the smallest model, not contend with the big local model on a serial
# endpoint. Titling routes to 'triage' (see api.py), so this set covers it.
_CHEAP_ROLES = frozenset({"triage", "enhancer"})


def cheap_model_for(role: str) -> str | None:
    """Cheap-tier model fallback for a cheap role.

    Returns ``AIFORGE_CHEAP_MODEL`` when: the role is a cheap role, the env is
    set, AND there is NO explicit per-role pin (neither ``AIFORGE_<ROLE>_MODEL``
    env nor a persisted per-role row carrying a ``model``). Otherwise ``None`` —
    the caller keeps today's resolution. Unset ``AIFORGE_CHEAP_MODEL`` → always
    ``None`` (fully backward compatible)."""
    if role not in _CHEAP_ROLES:
        return None
    cheap = (os.environ.get("AIFORGE_CHEAP_MODEL") or "").strip()
    if not cheap:
        return None
    # An explicit per-role pin (env or persisted per-role row) wins.
    if (os.environ.get(f"AIFORGE_{role.upper()}_MODEL") or "").strip():
        return None
    try:
        p = _path()
        if p.exists():
            disk = _fc.read_json(p) or {}
            row = disk.get(role)
            if isinstance(row, dict) and (row.get("model") or "").strip():
                return None
    except Exception:  # noqa: BLE001
        pass
    return cheap
