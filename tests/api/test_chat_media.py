"""Chat image attachments: upload → store → list → describe → delete, plus the
description reaching the agent context and NOT polluting the session summary."""
import importlib

import pytest
from fastapi.testclient import TestClient

# Smallest valid PNG (1x1) — magic bytes pass vision._detect_mime.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_CHAT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
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
    return TestClient(api.app), api


def _new_session(client) -> int:
    return client.post("/api/chat/sessions", json={"title": "t"}).json()["id"]


def test_upload_list_describe_delete(app_client):
    client, _ = app_client
    sid = _new_session(client)

    # Upload (no vision model in test → no auto-description; manual caption).
    r = client.post(f"/api/chat/sessions/{sid}/media",
                    files={"file": ("shot.png", _PNG, "image/png")})
    assert r.status_code == 201, r.text
    media = r.json()
    assert media["filename"].endswith(".png") and media["mime"] == "image/png"
    assert media["auto_described"] is False
    mid = media["id"]

    # List + vision flag present.
    lst = client.get(f"/api/chat/sessions/{sid}/media").json()
    assert len(lst["media"]) == 1 and lst["vision"] is False

    # Add a caption → it becomes the queryable description.
    pr = client.patch(f"/api/chat/media/{mid}",
                      json={"description": "a login screen with a red error"})
    assert pr.json()["description"] == "a login screen with a red error"

    # Raw bytes are served.
    raw = client.get(f"/api/chat/media/{mid}/raw")
    assert raw.status_code == 200 and raw.content[:4] == b"\x89PNG"

    # Delete removes the row + file.
    assert client.delete(f"/api/chat/media/{mid}").status_code == 204
    assert client.get(f"/api/chat/sessions/{sid}/media").json()["media"] == []


def test_context_block_carries_description(app_client):
    client, _ = app_client
    sid = _new_session(client)
    mid = client.post(f"/api/chat/sessions/{sid}/media",
                      files={"file": ("c.png", _PNG, "image/png")}).json()["id"]
    client.patch(f"/api/chat/media/{mid}",
                 json={"description": "UNIQUEMARKER bar chart"})
    from aiforge_core.runtime import chat_media
    block = chat_media.context_block(sid)
    assert "SESSION IMAGES" in block and "UNIQUEMARKER bar chart" in block


def test_reject_non_image(app_client):
    client, _ = app_client
    sid = _new_session(client)
    r = client.post(f"/api/chat/sessions/{sid}/media",
                    files={"file": ("x.txt", b"not an image", "text/plain")})
    assert r.status_code == 400


def test_vision_setting_toggles_capability(app_client):
    client, _ = app_client
    client.put("/api/runtime/llm-settings", json={"vision_capable": 1})
    from aiforge_core.runtime import chat_media
    assert chat_media.vision_enabled("chat") is True
    client.put("/api/runtime/llm-settings", json={"vision_capable": 0})
    assert chat_media.vision_enabled("chat") is False


def test_vision_probe_accepts_and_rejects(app_client, monkeypatch):
    """No hardcoded allowlist — capability is PROBED from the endpoint. A server
    that accepts the image → vision; one that rejects image content → not."""
    from aiforge_core.runtime import chat_media
    from aiforge_core.llm import client as llm

    # Accepting server → vision True (cached per model).
    chat_media.reset_vision_cache()
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "ok")
    assert chat_media._probe_vision("some-served-model", "chat") is True
    assert chat_media._probe_vision("some-served-model", "chat") is True  # cached

    # Rejecting server (image content invalid) → vision False.
    chat_media.reset_vision_cache()
    def _reject(*a, **k):
        raise RuntimeError("400 invalid image content for this model")
    monkeypatch.setattr(llm, "complete", _reject)
    assert chat_media._probe_vision("text-only-model", "chat") is False
