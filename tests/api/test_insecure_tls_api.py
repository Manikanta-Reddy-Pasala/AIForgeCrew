"""API wire-contract for the per-endpoint TLS opt-out (`insecure_tls`).

Locks the surface the Home/Setup UI depends on:
  PUT  /api/agents/v2/{role}/config  accepts + echoes insecure_tls
  GET  /api/agents/v2/config         reports insecure_tls per role
  POST /api/providers/test           forwards insecure_tls to the probe
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    for k in ("AIFORGE_MEMORY_BACKEND", "AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app)


def test_v2_set_and_get_roundtrips_insecure_tls(client):
    r = client.put("/api/agents/v2/doer/config", json={
        "provider": "openai_compatible",
        "model": "my-model",
        "base_url": "https://chatai.internal",
        "insecure_tls": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["insecure_tls"] is True

    cfg = client.get("/api/agents/v2/config").json()
    assert cfg["doer"]["insecure_tls"] is True
    # default stays False for an untouched role
    assert cfg["planner"]["insecure_tls"] is False


def test_providers_test_forwards_insecure(client, monkeypatch):
    captured = {}

    def _fake_probe(base_url, api_key=None, insecure=False):
        captured["insecure"] = insecure
        return {"ok": True, "models": ["m"]}

    import aiforge_core.llm.providers.openai_compatible as oc
    monkeypatch.setattr(oc, "probe", _fake_probe)

    r = client.post("/api/providers/test", json={
        "base_url": "https://chatai.internal",
        "insecure_tls": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert captured["insecure"] is True
