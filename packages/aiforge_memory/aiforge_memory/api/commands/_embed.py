"""Best-effort text embedding via the bge-m3 sidecar.

Returns ``None`` on any failure so memory writes degrade gracefully
when the sidecar is offline. Callers MUST treat ``None`` as a
non-fatal "no embedding written" outcome rather than re-trying.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def embed_text(text: str) -> list[float] | None:
    url = os.environ.get(
        "AIFORGE_EMBED_URL", "http://127.0.0.1:8764"
    ).rstrip("/")
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url + "/embed", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        vec = data.get("embedding") or []
        return [float(x) for x in vec] if vec else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


__all__ = ["embed_text"]
