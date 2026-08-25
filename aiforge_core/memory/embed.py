"""Single embed() helper talking to the bge-m3 sidecar on :8764.

Replaces the LM Studio nomic-embed endpoint used by old pgmem.py.
All tiers (T1–T4) embed through this one helper.
"""
from __future__ import annotations

import json
import os
import urllib.request

from aiforge_core.net.ssl import context_for as _ssl_context_for

_EMBED_BATCH = '/embed_batch'

SIDECAR_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
DIM = 1024


def _post(path: str, body: dict, timeout: float | None = None) -> dict:
    """POST to the embed sidecar. Auto-scales timeout with batch size:
    a 60-doc batch on CPU takes ~30-90s; the old 20s default fired
    fallback paths in unified_query._similar_tickets (cosines came
    back as 0.000 for every row).
    """
    if timeout is None:
        n = len(body.get("texts") or []) if path == _EMBED_BATCH else 1
        # Linear scale: 5s base + 1.5s per doc, capped at 180s.
        timeout = min(180.0, 5.0 + 1.5 * max(1, n))
    req = urllib.request.Request(
        f"{SIDECAR_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    ctx = _ssl_context_for(f"{SIDECAR_URL}{path}")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())


def embed(text: str) -> list[float]:
    """Return 1024-d dense embedding for `text`."""
    if not text.strip():
        raise ValueError("cannot embed empty text")
    resp = _post("/embed", {"text": text})
    return resp["embedding"]


def embed_batch(texts: list[str], *, chunk_size: int = 16) -> list[list[float]]:
    """Return list of 1024-d embeddings. Preserves input order.

    Chunks at the client to keep individual HTTP calls bounded (~15s
    each on CPU). Single 61-doc batch was timing out at the urllib
    socket layer even at 180s; chunked it completes in ~30s wall
    with predictable per-chunk timeouts."""
    if not texts:
        return []
    if len(texts) <= chunk_size:
        resp = _post(_EMBED_BATCH, {"texts": list(texts)})
        return resp["embeddings"]
    out: list[list[float]] = []
    for i in range(0, len(texts), chunk_size):
        chunk = list(texts[i:i + chunk_size])
        resp = _post(_EMBED_BATCH, {"texts": chunk})
        out.extend(resp["embeddings"])
    return out
