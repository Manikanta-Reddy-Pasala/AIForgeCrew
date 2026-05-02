"""Provider health probe with TTL cache.

Cursor / Claude Code don't waste a round-trip on a dead local server —
they probe ``/v1/models`` (or equivalent) once and cache the verdict
for ~30 seconds. This module does the same for the AIForge router so
``router.resolve()`` and ``router.fallback()`` can skip providers we
already know are down.

The probe is deliberately conservative:

* GET to ``{base_url}/models`` with the provider's auth header.
* 2-second hard timeout — health checks must never block hot paths.
* Connection refused / DNS fail / 5xx → DOWN for the cache window.
* Anything 2xx/3xx/4xx (i.e. server replied) → UP.

Disable via ``AIFORGE_HEALTH_DISABLE=1``.
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .types import Endpoint
from . import providers as _providers


_DEFAULT_TTL_S = 30.0
_DEFAULT_TIMEOUT_S = 2.0


@dataclass
class HealthState:
    up: bool
    checked_at: float
    reason: str = ""


_CACHE: dict[str, HealthState] = {}


def _ttl() -> float:
    raw = os.environ.get("AIFORGE_HEALTH_TTL_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_TTL_S


def _timeout() -> float:
    raw = os.environ.get("AIFORGE_HEALTH_TIMEOUT_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_S


def _disabled() -> bool:
    return os.environ.get("AIFORGE_HEALTH_DISABLE", "0") in ("1", "true")


def _probe(ep: Endpoint) -> HealthState:
    """One-shot probe. Caller is expected to cache the result."""
    url = f"{ep.base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {ep.api_key or 'na'}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            # Any HTTP status reaching us means the server is alive.
            return HealthState(up=True, checked_at=time.time(),
                               reason=f"http_{resp.status}")
    except urllib.error.HTTPError as exc:
        # 401/403/404 still proves the server is reachable.
        return HealthState(up=True, checked_at=time.time(),
                           reason=f"http_{exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return HealthState(up=False, checked_at=time.time(),
                           reason=f"{type(exc).__name__}: {exc}"[:160])


def is_up(provider_name: str, *, role: str = "doer") -> bool:
    """Return cached health for ``provider_name``.

    Health is keyed by provider name (not endpoint URL) — same provider
    serves all roles in this system. ``role`` is only used to construct
    a probe Endpoint when no cached value exists.
    """
    if _disabled():
        return True
    cached = _CACHE.get(provider_name)
    now = time.time()
    if cached is not None and (now - cached.checked_at) < _ttl():
        return cached.up
    prov = _providers.get(provider_name)
    if prov is None:
        _CACHE[provider_name] = HealthState(
            up=False, checked_at=now, reason="unknown_provider")
        return False
    if not prov.is_available():
        # Missing API key etc. — short-circuit DOWN without network.
        _CACHE[provider_name] = HealthState(
            up=False, checked_at=now, reason="not_available")
        return False
    ep = prov.endpoint(role)
    state = _probe(ep)
    _CACHE[provider_name] = state
    return state.up


def snapshot() -> dict[str, dict]:
    """Inspect cache — used by /api/runtime/health."""
    return {
        name: {"up": s.up, "age_s": round(time.time() - s.checked_at, 2),
               "reason": s.reason}
        for name, s in _CACHE.items()
    }


def invalidate(provider_name: str | None = None) -> None:
    """Drop one entry (or all) — useful after fixing a known outage."""
    if provider_name is None:
        _CACHE.clear()
    else:
        _CACHE.pop(provider_name, None)
