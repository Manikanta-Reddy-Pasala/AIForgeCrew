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


log = logging.getLogger("aiforge.local_probe")


# Cache: ``{api_base: (alive?, expires_at)}``. Module-level so every
# pipeline build in this process reuses the result.
_CACHE: dict[str, tuple[bool, float]] = {}


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
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            alive = 200 <= resp.status < 500
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError) as exc:
        log.info("local_probe: %s unreachable (%s)", api_base,
                 type(exc).__name__)
        alive = False

    _CACHE[api_base] = (alive, now + _ttl())
    if alive:
        log.debug("local_probe: %s alive", api_base)
    return alive


def maybe_substitute_primary(role: str, primary_cfg: dict) -> dict:
    """If ``primary_cfg`` points at a dead local endpoint, swap it for
    a cloud default. Returns the original dict unchanged otherwise.

    Substitution rule: only acts on the ``local`` provider. Cloud
    providers always go through; ``claude_local`` is a subscription
    CLI not an HTTP endpoint and isn't probeable from here. The
    cloud default is read from
    :func:`aiforge_core.config.agent_config.cloud_default_for_local`
    so the operator can pin a different fallback (e.g. anthropic) via
    env without code changes.
    """
    if primary_cfg.get("_claude_cli"):
        return primary_cfg
    api_base = primary_cfg.get("api_base", "")
    # mlx-lm / LM Studio defaults to localhost:1234. The local provider
    # is the only one that can plausibly be off; everything else has
    # an SLA from a remote service.
    is_local = (
        "127.0.0.1:1234" in api_base
        or "localhost:1234" in api_base
        or "10.10.10" in api_base  # operator's direct-LAN Mac Studio
    )
    if not is_local:
        return primary_cfg
    if is_alive(api_base):
        return primary_cfg

    # Local is dead — try to bring it up via SSH before falling back.
    # Auto-start is opt-in (needs AIFORGE_LMS_HOST configured) and
    # only runs once per process; if it succeeds we keep the local
    # primary cfg and the rest of the pipeline runs on fast mlx-lm.
    try:
        from .local_starter import try_start as _try_start
        if _try_start(api_base):
            log.info(
                "local_probe: %s back online via lms_autostart, "
                "keeping local primary for role=%s", api_base, role,
            )
            return primary_cfg
    except Exception as exc:  # noqa: BLE001 — never break ticket flow
        log.warning("local_probe: auto-start raised: %s", exc)

    # Auto-start declined / failed → fall back to cloud default.
    try:
        from aiforge_core.config.agent_config import cloud_default_for_local
        substitute = cloud_default_for_local(role)
    except Exception as exc:
        log.warning("local_probe: cloud_default lookup failed: %s", exc)
        return primary_cfg
    if substitute is None:
        log.warning(
            "local_probe: %s dead but no cloud default available "
            "for role=%s — keeping dead primary, chain will rescue",
            api_base, role,
        )
        return primary_cfg

    log.info(
        "local_probe: %s dead → substituting primary for role=%s with "
        "%s (%s)", api_base, role,
        substitute.get("_provider", "cloud"),
        substitute.get("model_id"),
    )
    return substitute


__all__ = ["is_alive", "maybe_substitute_primary"]
