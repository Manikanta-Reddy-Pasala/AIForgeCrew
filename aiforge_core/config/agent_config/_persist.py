"""Persistence: set_role / reset for ``agent_config`` (split submodule)."""
from __future__ import annotations

import threading
from typing import Any

from ._resolve import get
from ._state import (
    _CATALOG_CACHE,
    _CATALOG_LOCK,
    _DEFAULT_KEY,
    _LOCAL_DEFAULT_CACHE,
    _ROLES,
    _fc,
    _path,
    PROVIDERS,
)

_LOCK = threading.Lock()


def set_role(role: str, provider: str, model: str,
             base_url: str | None = None,
             api_key: str | None = None,
             insecure_tls: bool = False) -> dict[str, Any]:
    """Persist {provider, model, base_url?, api_key?, insecure_tls?}.

    ``base_url`` is optional; when None, the provider's default is used at
    resolve time. ``api_key`` is optional too — used mainly by the
    ``openai_compatible`` provider for cloud-with-key endpoints; leave it
    blank for OSS-no-token. ``insecure_tls`` skips TLS verification for
    this endpoint only (self-signed / internal HTTPS box) — a per-role
    opt-out that avoids editing env files + restarting. Env vars still win
    on next read, which is desired for a one-off override without losing
    the saved default.
    """
    if role != _DEFAULT_KEY and role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not model or not model.strip():
        raise ValueError("model cannot be empty")
    if base_url is not None and not isinstance(base_url, str):
        raise ValueError("base_url must be string or None")
    if api_key is not None and not isinstance(api_key, str):
        raise ValueError("api_key must be string or None")
    with _LOCK:
        p = _path()
        disk: dict[str, dict[str, Any]] = {}
        if p.exists():
            try:
                disk = _fc.read_json(p) or {}
            except Exception:
                disk = {}
        row: dict[str, Any] = {
            "provider": provider, "model": model.strip(),
        }
        if base_url and base_url.strip():
            row["base_url"] = base_url.strip()
        else:
            row["base_url"] = None
        # Secret-preserving: the UI never echoes a stored api_key back, so
        # its field is blank on every reload. A blank key here therefore
        # means "leave the saved token untouched", NOT "wipe it" — else a
        # plain Save (or per-row Save after Apply-to-all) would silently
        # null the token. Pass api_key="" explicitly to clear (UI sends a
        # non-empty value only when the operator typed a new token).
        if api_key and api_key.strip():
            row["api_key"] = api_key.strip()
        else:
            row["api_key"] = (disk.get(role) or {}).get("api_key")
        row["insecure_tls"] = bool(insecure_tls)
        disk[role] = row
        _fc.write_json(p, disk)   # atomic + busts the read cache
    return get(role)


def reset(*, keep_default: bool = False) -> dict:
    """Wipe the persisted per-role config for a clean reconfigure.

    Deletes ``agent_config.json`` so every role reverts to defaults (the
    global ``_default`` if env-set, else the neutral local placeholder) — the
    operator then sets one endpoint fresh, with no stale per-role rows
    shadowing it. ``keep_default=True`` preserves the global ``_default`` row
    and clears only the per-role rows. Returns ``{ok, removed, path}``.
    """
    # Drop in-process caches so a reconfigure right after a reset isn't served
    # a stale local-model id / catalog from the 5-minute TTL caches.
    _LOCAL_DEFAULT_CACHE[0] = 0.0
    _LOCAL_DEFAULT_CACHE[1] = None
    with _CATALOG_LOCK:
        _CATALOG_CACHE.clear()
    _fc.clear()
    with _LOCK:
        p = _path()
        if not p.exists():
            return {"ok": True, "removed": False, "path": str(p),
                    "note": "no saved config to reset"}
        if keep_default:
            # NEVER delete the file in keep_default mode — strip only the
            # per-role rows, preserving the global _default (write back an
            # empty/`{}` map when there was no _default, so the request is
            # honoured exactly rather than nuking everything).
            try:
                disk = _fc.read_json(p) or {}
            except Exception:  # noqa: BLE001
                disk = {}
            kept = {k: v for k, v in disk.items() if k == _DEFAULT_KEY}
            _fc.write_json(p, kept)
            return {"ok": True, "removed": "per-role rows", "path": str(p),
                    "kept_default": bool(kept)}
        try:
            p.unlink()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "path": str(p)}
        return {"ok": True, "removed": True, "path": str(p)}
