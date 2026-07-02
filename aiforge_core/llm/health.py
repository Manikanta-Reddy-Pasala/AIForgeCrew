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

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .types import Endpoint
from . import providers as _providers
from ._ssl import context_for as _ssl_context_for


_DEFAULT_TTL_S = 30.0
_DEFAULT_TIMEOUT_S = 2.0

# Ceiling for a detected window (256K) — we never trust a model that reports
# more, and everything downstream is sized against this bound.
_CTX_CEILING = 262144
# Fields a served model exposes its context length under, in priority order:
# vLLM (``max_model_len``), generic (``context_length``), LM Studio
# (``loaded_context_length``).
_CTX_FIELDS = ("max_model_len", "context_length", "loaded_context_length")


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
    ctx = _ssl_context_for(ep.base_url)
    try:
        with urllib.request.urlopen(req, timeout=_timeout(), context=ctx) as resp:
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


# ── context-window auto-detect (Fix B) ──────────────────────────────────
# A model loaded with a 256K window is treated as the static default until an
# operator hand-sets it. Probing ``/v1/models`` reads the model's REAL window
# so the budgets can use it. Cached per endpoint (short TTL) so it never adds
# per-turn latency and soft-fails (→ None) to today's static behaviour.
_CTX_CACHE: dict[str, tuple[float, int | None]] = {}


def _ctx_timeout() -> float:
    # Never block a turn: cap the models GET at ~1.5s (or the health timeout).
    # A reachable endpoint answers /v1/models near-instantly; a down/absent one
    # should fail FAST (C2 — the suite + prod hot-path stop thrashing on 2-3s).
    return min(_timeout(), 1.5)


def _ctx_neg_ttl() -> float:
    """TTL for a NEGATIVE (None/unreachable) context-probe result. Much longer
    than the positive TTL so a down/absent endpoint isn't re-probed every turn
    (C2). Env ``AIFORGE_CTX_PROBE_NEG_TTL_S`` (default 600s)."""
    raw = os.environ.get("AIFORGE_CTX_PROBE_NEG_TTL_S")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 600.0


def _extract_ctx_len(body: object) -> int | None:
    """Pull the model's advertised context length from a ``/v1/models`` body.
    Handles the OpenAI-style ``{"data": [ {...} ]}`` envelope and a bare model
    dict. Returns the capped int, or None when no known field is present."""
    try:
        entry: object = None
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data:
                entry = data[0]
            else:
                entry = body
        if not isinstance(entry, dict):
            return None
        for field in _CTX_FIELDS:
            v = entry.get(field)
            if isinstance(v, bool):        # guard: bool is an int subclass
                continue
            if isinstance(v, (int, float)) and v > 0:
                return min(int(v), _CTX_CEILING)
    except Exception:  # noqa: BLE001
        return None
    return None


def _probe_ctx_window(base_url: str) -> int | None:
    url = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": "Bearer na"})
    ctx = _ssl_context_for(base_url)
    try:
        with urllib.request.urlopen(req, timeout=_ctx_timeout(), context=ctx) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — soft-fail: any error → unknown
        return None
    return _extract_ctx_len(body)


def probe_context_window(base_url: str) -> int | None:
    """Detected input context window (tokens) for the model served at
    ``base_url``, read from ``/v1/models`` (``max_model_len`` /
    ``context_length`` / ``loaded_context_length``), capped at 256K. Returns
    None on any miss/error. Cached per endpoint for the health TTL so it costs
    at most one short GET per window and never blocks a hot path."""
    key = (base_url or "").rstrip("/")
    if not key:
        return None
    now = time.time()
    hit = _CTX_CACHE.get(key)
    if hit is not None:
        ts, cached = hit
        # C2: hold a NEGATIVE result far longer than a positive one so an
        # absent/down endpoint isn't re-probed every 30s (thrashing the suite
        # + a prod hot-path on the default config).
        ttl = _ctx_neg_ttl() if cached is None else _ttl()
        if (now - ts) < ttl:
            return cached
    val = _probe_ctx_window(key)
    _CTX_CACHE[key] = (now, val)
    return val


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
        _CTX_CACHE.clear()
    else:
        _CACHE.pop(provider_name, None)
