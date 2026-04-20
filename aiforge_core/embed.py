"""Single embed() helper talking to the bge-m3 sidecar on :8764.

Replaces the LM Studio nomic-embed endpoint used by old pgmem.py.
All tiers (T1–T4) embed through this one helper.
"""
from __future__ import annotations

import json
import os
import urllib.request

SIDECAR_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
DIM = 1024


def _post(path: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        f"{SIDECAR_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def embed(text: str) -> list[float]:
    """Return 1024-d dense embedding for `text`."""
    if not text.strip():
        raise ValueError("cannot embed empty text")
    resp = _post("/embed", {"text": text})
    return resp["embedding"]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Return list of 1024-d embeddings. Preserves input order."""
    if not texts:
        return []
    resp = _post("/embed_batch", {"texts": list(texts)})
    return resp["embeddings"]
