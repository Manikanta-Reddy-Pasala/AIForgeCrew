"""The two read-only sync endpoints."""
from __future__ import annotations

import hashlib
import importlib

from fastapi.testclient import TestClient


def _fresh_api(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN", "AIFORGE_BIND_HOST"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return api


def _seed_capture(tmp_path, text: str) -> str:
    d = tmp_path / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a-20260719-aaaaaa.md").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


def test_manifest_returns_entries_and_roster(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get("/api/memory/sync/manifest")

    assert r.status_code == 200
    body = r.json()
    assert [e["hash"] for e in body["manifest"]] == [digest]
    assert body["roster"][0]["id"] == "book"


def test_blob_returns_bytes_for_an_advertised_hash(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get(f"/api/memory/sync/blob/{digest}")

    assert r.status_code == 200
    assert r.content == b"hello"


def test_blob_404s_for_an_unknown_hash(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    _seed_capture(tmp_path, "hello")

    r = TestClient(api.app).get("/api/memory/sync/blob/" + "0" * 64)

    assert r.status_code == 404


def test_endpoints_require_the_api_token_when_one_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cret")
    api = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cret")
    client = TestClient(api.app)

    assert client.get("/api/memory/sync/manifest").status_code == 401
    ok = client.get("/api/memory/sync/manifest",
                    headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
