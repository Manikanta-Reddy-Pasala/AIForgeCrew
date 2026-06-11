"""Contract test for embed sidecar. Requires sidecar running at :8764."""
from __future__ import annotations
import os
import urllib.request
import json
import pytest

SIDECAR = os.environ.get("EMBED_SIDECAR_URL", "http://127.0.0.1:8764")


def _sidecar_up() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR}/healthz", timeout=2)
        return True
    except Exception:
        return False


# Live contract test — hermetic skip when the sidecar isn't running so
# the suite is green on dev boxes without the service stack.
pytestmark = pytest.mark.skipif(
    not _sidecar_up(),
    reason=f"embed sidecar not reachable at {SIDECAR}; "
           "start services/embed_sidecar to run this contract test",
)


def _post(path, body):
    req = urllib.request.Request(
        f"{SIDECAR}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@pytest.mark.live_sidecar
def test_embed_returns_1024_vector():
    resp = _post("/embed", {"text": "hello world"})
    assert "embedding" in resp
    assert len(resp["embedding"]) == 1024
    assert all(isinstance(x, float) for x in resp["embedding"])


@pytest.mark.live_sidecar
def test_embed_batch():
    resp = _post("/embed_batch", {"texts": ["a", "b", "c"]})
    assert len(resp["embeddings"]) == 3
    assert len(resp["embeddings"][0]) == 1024
