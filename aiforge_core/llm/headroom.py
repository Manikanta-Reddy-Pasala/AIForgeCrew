"""Headroom compression as a transparent forwarding proxy — one mechanism for
EVERY agent.

Earlier this lived in two scattered places: a compress-only call inside
``llm/client.py`` (covered the ReAct / text-doer path) and an ADK
``before_model_callback`` (covered the native-tools doer). Two code paths, two
things to keep in sync, and the ADK one never even fired on the local text-doer.

This replaces both with the classic proxy pattern: the headroom sidecar runs in
FORWARDING mode (compress → forward to the real model), and the app simply
repoints the local model endpoint at it. Because BOTH resolver boundaries —
``router.resolve`` (client.py) and ``agent_config.resolve_litellm`` (ADK
LiteLlm) — pass their ``base_url`` through :func:`proxy_base`, every agent role
(architect, planner, doer, refiner, verifier, chat, enhancer, …) transparently
gets compression with zero per-call code. Nothing else in the app knows the
proxy exists; turn the flag off and every base_url resolves straight to the
model again.

Scope: only the ONE endpoint the proxy fronts (``AIFORGE_HEADROOM_UPSTREAM``,
default the loopback LM Studio at ``127.0.0.1:1234``) is redirected. Any other
endpoint — a cloud escalation target, a different local box — is returned
unchanged, so a fast metered cloud call never detours through the compressor and
a second local endpoint is never misforwarded to the wrong upstream.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

__all__ = ["enabled", "proxy_base", "upstream", "proxy_url"]


def enabled() -> bool:
    return os.environ.get("AIFORGE_HEADROOM", "0") in ("1", "true", "yes", "on")


def proxy_url() -> str:
    """Where the headroom proxy listens (host root, no path)."""
    return os.environ.get("AIFORGE_HEADROOM_URL", "http://127.0.0.1:8787").rstrip("/")


def upstream() -> str:
    """The single real model endpoint the proxy forwards to — and therefore the
    ONLY endpoint the app redirects through it. Defaults to the loopback LM
    Studio. Compare on host:port only, so ``/v1`` vs no-``/v1`` never matters."""
    return os.environ.get("AIFORGE_HEADROOM_UPSTREAM", "http://127.0.0.1:1234").rstrip("/")


def _hostport(url: str) -> tuple[str | None, int | None]:
    try:
        u = urlparse(url if "://" in url else f"http://{url}")
        return (u.hostname, u.port)
    except Exception:  # noqa: BLE001
        return (None, None)


def proxy_base(base_url: str | None) -> str | None:
    """Return the headroom proxy URL in place of ``base_url`` when ``base_url``
    is the fronted local endpoint and compression is on; otherwise return
    ``base_url`` unchanged. Preserves the original path (e.g. ``/v1``), swapping
    only host:port — so the proxy exposes the exact same API surface the model
    did. Strict no-op when the flag is off, on any parse error, or for any
    endpoint that isn't the fronted one."""
    if not base_url or not enabled():
        return base_url
    try:
        target = _hostport(base_url)
        if target == (None, None) or target != _hostport(upstream()):
            return base_url
        pu = urlparse(proxy_url())
        u = urlparse(base_url)
        # Swap scheme://host:port → proxy's; keep path/query untouched.
        return base_url.replace(f"{u.scheme}://{u.netloc}",
                                f"{pu.scheme}://{pu.netloc}", 1)
    except Exception:  # noqa: BLE001 — routing must never break resolution
        return base_url
