"""Fetching from one peer over HTTP. Knows nothing about disk.

An unreachable peer is normal operation, not an error: pull-only means nothing
is queued for it and nothing blocks on it, so every failure here degrades to
"nothing new this cycle" and is retried on the next one.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("aiforge.sync")

TIMEOUT = 20.0


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_manifest(base_url: str, token: str = "") -> dict:
    """GET a peer's manifest. Returns {} when the peer is unreachable."""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/memory/sync/manifest",
                      headers=_headers(token), timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — an unreachable peer is expected, not exceptional
        _log.info("sync: peer %s unreachable: %s", base_url, exc)
        return {}
    return data if isinstance(data, dict) else {}


def fetch_blob(base_url: str, digest: str, token: str = "") -> bytes | None:
    """GET one blob by hash. Returns None on any failure; retried next cycle."""
    import httpx

    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/memory/sync/blob/{digest}",
                      headers=_headers(token), timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as exc:  # noqa: BLE001 — retried on the next cycle
        _log.info("sync: blob %s from %s failed: %s", digest[:8], base_url, exc)
        return None


__all__ = ["fetch_manifest", "fetch_blob", "TIMEOUT"]
