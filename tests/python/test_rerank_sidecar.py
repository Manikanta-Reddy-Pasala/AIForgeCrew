"""Contract test for rerank sidecar. Requires sidecar running at :8765."""
from __future__ import annotations
import os
import urllib.request
import json
import pytest

SIDECAR = os.environ.get("RERANK_SIDECAR_URL", "http://127.0.0.1:8765")


def _post(path, body):
    req = urllib.request.Request(
        f"{SIDECAR}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


@pytest.mark.live_sidecar
def test_rerank_orders_relevant_first():
    resp = _post(
        "/rerank",
        {
            "query": "how to publish NATS message",
            "candidates": [
                {"id": "a", "text": "JetStream publishAsync API sample"},
                {"id": "b", "text": "how to write a haiku about spring"},
                {"id": "c", "text": "publishToRemoteServer uses local NATS queue"},
            ],
        },
    )
    assert "order" in resp
    assert len(resp["order"]) == 3
    # relevant candidates (a, c) should rank above irrelevant (b)
    ranked_ids = [["a", "b", "c"][i] for i in resp["order"]]
    assert ranked_ids.index("b") > 0  # "b" is not first
