"""Pre-flight liveness probe for the ``local`` provider.

LM Studio / mlx-lm runs on a separate host (Mac Studio over the
direct LAN). When that box is asleep or LM Studio isn't started, the
local primary endpoint refuses every request — so the EscalatingLlm
sticky-demotion fires on call #1 and the rest of the run pays cloud
latency on every Doer turn (ONE-109 wall-clock 866s vs ONE-108's 81s).

Fix: probe the local endpoint at pipeline-build time. If it doesn't
answer ``/v1/models`` within 2 seconds, swap the primary cfg to a
sensible cloud default (Ollama Cloud's ``qwen3-coder-next``) BEFORE
the first agent call. Result is the cloud serves directly as primary,
no wasted local round-trips.

Probe results are cached so the build doesn't pay the 2-second tax for
every archetype (5 LlmAgents × 2s = 10s extra per ticket build). TTL
defaults to 60s — long enough to amortise across one pipeline run,
short enough that an operator who just started LM Studio sees the
primary come back on the next ticket without a service restart.

Honours:
  ``AIFORGE_LOCAL_PROBE_TIMEOUT_S`` — per-call timeout (default 2.0)
  ``AIFORGE_LOCAL_PROBE_TTL_S``     — cache lifetime (default 60)
  ``AIFORGE_LOCAL_PROBE_DISABLE=1`` — skip probe + always trust cfg
"""
from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request

from aiforge_core.net.ssl import context_for as _ssl_context_for

log = logging.getLogger("aiforge.local_probe")


# Cache: ``{api_base: (alive?, expires_at)}``. Module-level so every
# pipeline build in this process reuses the result.
_CACHE: dict[str, tuple[bool, float]] = {}

# Endpoints we've already emitted the "dead, no rescue" WARNING for — the
# pipeline builds ~16 roles against the SAME dead local endpoint, so without
# this the operator gets the identical warning 16× per ticket. Reset whenever
# the probe cache for that endpoint expires (see :func:`is_alive`).
_DEAD_WARNED: set[str] = set()


def _timeout() -> float:
    raw = os.environ.get("AIFORGE_LOCAL_PROBE_TIMEOUT_S", "2.0")
    try:
        return float(raw)
    except ValueError:
        return 2.0


def _ttl() -> float:
    raw = os.environ.get("AIFORGE_LOCAL_PROBE_TTL_S", "60")
    try:
        return float(raw)
    except ValueError:
        return 60.0


def is_alive(api_base: str) -> bool:
    """Return True when ``{api_base}/models`` answers within the timeout.

    Empty / missing api_base counts as not-alive — caller falls back to
    the cloud default.
    """
    if not api_base:
        return False
    if os.environ.get("AIFORGE_LOCAL_PROBE_DISABLE", "0") in ("1", "true"):
        return True

    now = time.monotonic()
    cached = _CACHE.get(api_base)
    if cached and cached[1] > now:
        return cached[0]

    url = api_base.rstrip("/") + "/models"
    alive = False
    try:
        from aiforge_core.llm.user_agent import user_agent as _ua
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": _ua()})
        with urllib.request.urlopen(
            req, timeout=_timeout(), context=_ssl_context_for(url)
        ) as resp:
            alive = 200 <= resp.status < 500
    except OSError as exc:
        log.info("local_probe: %s unreachable (%s)", api_base,
                 type(exc).__name__)
        alive = False

    _CACHE[api_base] = (alive, now + _ttl())
    if alive:
        log.debug("local_probe: %s alive", api_base)
        _DEAD_WARNED.discard(api_base)   # recovered → allow a fresh warn later
    return alive


def maybe_substitute_primary(role: str, primary_cfg: dict) -> dict:
    """No-op now that ``openai_compatible`` is the only provider.

    This used to swap a dead local mlx-lm primary for a cloud default,
    but there is no ``local`` provider and no built-in cloud fallback
    anymore. Returns ``primary_cfg`` unchanged so existing callers
    (pipeline build) keep working without a behaviour change — when the
    configured endpoint is down, the per-call retry chain in
    ``EscalatingLlm`` surfaces the error. :func:`is_alive` is retained
    for the standalone liveness checks that still use it.
    """
    return primary_cfg


__all__ = ["is_alive", "maybe_substitute_primary"]
