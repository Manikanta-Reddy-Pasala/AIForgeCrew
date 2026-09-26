"""How many model requests may run at once — and so whether a turn may
overlap its independent model calls.

On a box that serves one request at a time (LM Studio with parallel 1, a
single mlx-lm process) a second call does not run alongside the first, it
queues behind it: overlapping the classifier with the enhancer only made the
classifier wait out its own timeout. On a server with several slots the same
overlap is free time. Callers ask :func:`llm_slots` or :func:`parallel_ok`
and keep their sequential path when the answer is one.

Resolution, first that yields a value wins:
  1. the model's own ``parallel`` in the model registry (> 0);
  2. ``llm_parallel`` in runtime settings / ``AIFORGE_LLM_PARALLEL`` — a
     number; 0 or ``auto`` means detect;
  3. a probe of the role's server (see :mod:`._slots_probe`), cached per
     endpoint and model, and re-run when the model registry changes;
  4. 1.
"""
from __future__ import annotations

import os
import threading
import time

from . import _slots_probe

#: The role interactive chat runs on when a caller does not say.
CHAT_ROLE = "chat"

_LOCK = threading.Lock()
#: (base_url, model, registry stamp) -> (checked_at, slots)
_CACHE: "dict[tuple, tuple[float, int]]" = {}
#: One probe per key at a time: concurrent first callers share it.
_KEY_LOCKS: "dict[tuple, threading.Lock]" = {}


def _ttl(slots: int) -> float:
    """How long a probe answer holds. A one-slot answer is also what an
    unreachable server gives, so it lives as long as the context probe's
    negative result; a multi-slot answer is re-checked sooner, since a model
    reloaded with parallel 1 must stop being overlapped."""
    raw = os.environ.get("AIFORGE_LLM_PARALLEL_TTL_S")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 300.0 if slots > 1 else 600.0


def configured() -> int:
    """The operator's global setting: N > 0, or 0 for auto."""
    try:
        from aiforge_core.config import runtime_settings
        return max(0, int(runtime_settings.get("llm_parallel")))
    except Exception:  # noqa: BLE001 — "auto" in the env var lands here too
        raw = (os.environ.get("AIFORGE_LLM_PARALLEL") or "").strip()
        try:
            return max(0, int(raw))
        except ValueError:
            return 0


def _endpoint(role: str) -> "tuple[str, str, str]":
    try:
        from .router import resolve
        ep = resolve(role)
        return ((getattr(ep, "base_url", "") or "").rstrip("/"),
                getattr(ep, "model", "") or "", getattr(ep, "api_key", "") or "")
    except Exception:  # noqa: BLE001
        return "", "", ""


def _registry_parallel(model: str, base_url: str) -> int:
    try:
        from aiforge_core.config import model_registry
        return max(0, int(model_registry.parallel_for(model, base_url)))
    except Exception:  # noqa: BLE001
        return 0


def _registry_stamp():
    try:
        from aiforge_core.config import model_registry
        return model_registry._stamp(model_registry._path())
    except Exception:  # noqa: BLE001
        return None


def _probed(base_url: str, model: str, api_key: str) -> int:
    # perf_counter, not monotonic: tests drive the turn deadline through a
    # fake monotonic clock, and a cache read must not spend that budget.
    key = (base_url, model, _registry_stamp())
    now = time.perf_counter()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < _ttl(hit[1]):
            return hit[1]
        klock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with klock:
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and time.perf_counter() - hit[0] < _ttl(hit[1]):
                return hit[1]
        try:
            n = max(1, int(_slots_probe.probe(base_url, model, api_key)))
        except Exception:  # noqa: BLE001 — a probe never breaks a caller
            n = 1
        with _LOCK:
            _CACHE[key] = (time.perf_counter(), n)
    return n


def llm_slots(role: str = CHAT_ROLE) -> int:
    """Concurrent requests the server behind ``role`` serves. Never raises;
    anything unknown is 1."""
    base_url, model, api_key = _endpoint(role)
    per_model = _registry_parallel(model, base_url) if model else 0
    if per_model > 0:
        return per_model
    n = configured()
    if n > 0:
        return n
    if not base_url:
        return 1
    return _probed(base_url, model, api_key)


def parallel_ok(*roles: str) -> bool:
    """True when one call per role can run at the same time without any of
    them queueing behind another: roles on different servers always can;
    roles sharing a server need that many slots on it."""
    groups: "dict[str, list[str]]" = {}
    for r in roles:
        groups.setdefault(_endpoint(r)[0] or "?", []).append(r)
    if len(roles) < 2:
        return llm_slots(roles[0]) > 1 if roles else False
    for members in groups.values():
        if len(members) > 1 and min(llm_slots(r) for r in members) < len(members):
            return False
    return True


def reset() -> None:
    """Forget every probe answer (tests, and after reloading models)."""
    with _LOCK:
        _CACHE.clear()
        _KEY_LOCKS.clear()


__all__ = ["CHAT_ROLE", "configured", "llm_slots", "parallel_ok", "reset"]
