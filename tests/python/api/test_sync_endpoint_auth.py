"""The P2P sync endpoints must not be readable by the LAN.

``/api/memory/sync/manifest`` enumerates every syncable memory file and
``/api/memory/sync/blob/{hash}`` hands back its bytes, so an unauthenticated
non-loopback caller could bulk-download the whole memory tree in two requests.

Contract exercised here (the middleware in ``api._require_token``):
  * loopback peer            → allowed with no token (it can read the files
    off disk anyway; that is the same boundary /admin already trusts)
  * remote peer              → needs the shared token, no exceptions
  * remote peer + forged     → X-Forwarded-For/X-Real-IP/Host/Forwarded
    claiming 127.0.0.1 must NOT buy loopback trust
  * /api/health              → open to everyone (UI shell + probes)
  * non-loopback bind, no token → refused at boot

``TestClient(app, client=(host, port))`` populates ``scope["client"]`` exactly
as a real socket would, so the production branch runs — nothing is
monkeypatched, which is the point: a test that stubs the predicate cannot
notice the predicate being wrong.
"""
from __future__ import annotations

import hashlib
import importlib

import pytest
from fastapi.testclient import TestClient

SYNC = "/api/memory/sync/manifest"
TOKEN = "sh4red-t0ken"

LOOPBACK = ("127.0.0.1", 51000)
REMOTE = ("192.168.70.42", 51000)
SPOOF_HEADERS = {
    "X-Forwarded-For": "127.0.0.1",
    "X-Real-IP": "127.0.0.1",
    "Host": "127.0.0.1:8799",
    "Forwarded": "for=127.0.0.1;host=127.0.0.1;proto=http",
}


def _fresh_api(monkeypatch, tmp_path, *, token: str | None = TOKEN):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_BIND_HOST",
              "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK"):
        monkeypatch.delenv(k, raising=False)
    if token is None:
        monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_API_TOKEN", token)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return api


def _seed_capture(tmp_path, text: str = "a memory note") -> str:
    d = tmp_path / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a-20260719-aaaaaa.md").write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode()).hexdigest()


# ── the gate ───────────────────────────────────────────────────────────────

def test_loopback_reads_manifest_without_a_token(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path)

    r = TestClient(api.app, client=LOOPBACK).get(SYNC)

    assert r.status_code == 200
    assert [e["hash"] for e in r.json()["manifest"]] == [digest]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_every_loopback_form_is_trusted(monkeypatch, tmp_path, host):
    api = _fresh_api(monkeypatch, tmp_path)
    assert TestClient(api.app, client=(host, 51000)).get(SYNC).status_code == 200


def test_remote_without_a_token_is_rejected(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    _seed_capture(tmp_path)

    r = TestClient(api.app, client=REMOTE).get(SYNC)

    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()


def test_remote_with_the_right_token_is_allowed(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    r = client.get(SYNC, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert [e["hash"] for e in r.json()["manifest"]] == [digest]

    # …and the blob route the manifest advertises (the bulk-download half).
    blob = client.get(f"/api/memory/sync/blob/{digest}",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert blob.status_code == 200


def test_remote_with_a_wrong_token_is_rejected(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(f"/api/memory/sync/blob/{digest}",
                      headers={"X-AIForge-Token": "wrong"}).status_code == 401


def test_forged_loopback_headers_do_not_grant_access(monkeypatch, tmp_path):
    """The headline case: loopback is decided from the TCP peer only.

    Every header a reverse proxy would normally use to report the real client
    is settable by the client itself, so a remote attacker can claim
    127.0.0.1. There is no trusted proxy in front of this app.
    """
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC, headers=SPOOF_HEADERS).status_code == 401
    assert client.get(f"/api/memory/sync/blob/{digest}",
                      headers=SPOOF_HEADERS).status_code == 401
    # One header at a time, in case only one of them is consulted.
    for name, value in SPOOF_HEADERS.items():
        assert client.get(SYNC, headers={name: value}).status_code == 401, name


def test_health_stays_open_for_remote_callers(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    assert TestClient(api.app, client=REMOTE).get("/api/health").status_code == 200
    assert TestClient(api.app, client=LOOPBACK).get("/api/health").status_code == 200


# ── the boot guard, now the only thing between a LAN and the memory tree ───

def test_boot_guard_still_refuses_non_loopback_bind_without_a_token(
        monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, token=None)
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", raising=False)
    with pytest.raises(RuntimeError, match="non-loopback"):
        api._security_boot_guard()


def test_boot_guard_escape_hatch_survives(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, token=None)
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "1")
    api._security_boot_guard()  # must not raise
