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


def _fresh_api(monkeypatch, tmp_path, *, token: str | None = TOKEN,
               mesh_key: str | None = None):
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
    if mesh_key is None:
        monkeypatch.delenv("AIFORGE_MESH_KEY", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_MESH_KEY", mesh_key)
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


# ── the shared mesh key: sync-scoped, never a shell ────────────────────────
#
# AIFORGE_MESH_KEY authenticates a peer against the pull-only sync routes and
# NOTHING else. Its whole reason to exist is shared-key auto-join: a machine
# holding the key joins the mesh by itself, so the key must not double as the
# control-plane (shell/config) credential the API token is.

MESH = "mesh-" + "s3cret" * 4   # >= 24 chars, past the boot-guard floor


def test_mesh_key_reads_the_sync_manifest_from_the_lan(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, mesh_key=MESH)
    digest = _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    r = client.get(SYNC, headers={"Authorization": f"Bearer {MESH}"})
    assert r.status_code == 200
    assert [e["hash"] for e in r.json()["manifest"]] == [digest]

    blob = client.get(f"/api/memory/sync/blob/{digest}",
                      headers={"X-AIForge-Token": MESH})
    assert blob.status_code == 200


def test_mesh_key_does_NOT_open_the_control_plane(monkeypatch, tmp_path):
    """The security crux: the key that lets a peer sync must not let it run a
    shell or rewrite config. A non-sync route rejects the mesh key outright."""
    api = _fresh_api(monkeypatch, tmp_path, mesh_key=MESH)
    client = TestClient(api.app, client=REMOTE)

    for path in ("/api/config/agents", "/api/mcp/servers", "/api/chat/sessions",
                 "/api/tickets", "/api/repos"):
        r = client.get(path, headers={"Authorization": f"Bearer {MESH}"})
        assert r.status_code == 401, f"{path} accepted the mesh key: {r.status_code}"


def test_api_token_still_opens_both_surfaces(monkeypatch, tmp_path):
    """The API token is a superset — it authenticates every route, sync too."""
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key=MESH)
    _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC, headers={"Authorization": f"Bearer {TOKEN}"}
                      ).status_code == 200
    assert client.get("/api/config/agents",
                      headers={"Authorization": f"Bearer {TOKEN}"}
                      ).status_code == 200


def test_wrong_mesh_key_is_rejected_on_sync(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, mesh_key=MESH)
    _seed_capture(tmp_path)
    r = TestClient(api.app, client=REMOTE).get(
        SYNC, headers={"Authorization": "Bearer not-the-key"})
    assert r.status_code == 401


def test_challenge_is_reachable_unauth_and_proves_the_key(monkeypatch, tmp_path):
    """The auto-join handshake: /challenge is the one sync route open without a
    credential (it returns only an HMAC, never data), and its proof is
    HMAC(mesh_key, nonce) — so a peer can verify membership without either side
    transmitting the key."""
    import hashlib
    import hmac
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key=MESH)
    r = TestClient(api.app, client=REMOTE).get(
        "/api/memory/sync/challenge", params={"nonce": "n0nce"})
    assert r.status_code == 200          # reachable with NO token from the LAN
    expected = hmac.new(MESH.encode(), b"n0nce", hashlib.sha256).hexdigest()
    assert r.json()["proof"] == expected


def test_challenge_404s_when_no_mesh_key_is_configured(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key=None)
    r = TestClient(api.app, client=REMOTE).get(
        "/api/memory/sync/challenge", params={"nonce": "n0nce"})
    assert r.status_code == 404          # nothing to prove, and says so (not 401)


def test_sync_is_gated_by_mesh_key_even_with_no_api_token(monkeypatch, tmp_path):
    """A box that sets ONLY the mesh key (control plane loopback-only) must
    still refuse an unauthenticated remote on the sync routes — the mesh key
    protects them on its own."""
    api = _fresh_api(monkeypatch, tmp_path, token=None, mesh_key=MESH)
    _seed_capture(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC).status_code == 401
    assert client.get(SYNC, headers={"Authorization": f"Bearer {MESH}"}
                      ).status_code == 200


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


# ── mesh-key boot hardening ────────────────────────────────────────────────

def test_boot_guard_refuses_a_weak_mesh_key(monkeypatch, tmp_path):
    """The challenge oracle brute-forces a short key offline, and the key is
    auto-join, so a weak one is refused at boot."""
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key="short")
    with pytest.raises(RuntimeError, match="MESH_KEY is too short"):
        api._security_boot_guard(hosts=["127.0.0.1"])


def test_boot_guard_allows_a_strong_mesh_key(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key="x" * 32)
    api._security_boot_guard(hosts=["127.0.0.1"])  # must not raise


def test_boot_guard_refuses_mesh_key_equal_to_api_token(monkeypatch, tmp_path):
    """Same value = the sync-scoped key becomes the control-plane token, i.e.
    every sync peer gets a shell. Refused."""
    same = "x" * 32
    api = _fresh_api(monkeypatch, tmp_path, token=same, mesh_key=same)
    with pytest.raises(RuntimeError, match="equals AIFORGE_API_TOKEN"):
        api._security_boot_guard(hosts=["127.0.0.1"])


def test_mesh_key_does_not_authenticate_a_traversal_path(monkeypatch, tmp_path):
    """`_is_sync_path` rejects dot-segments/encoded traversal, so the mesh key
    cannot authenticate a path a fronting proxy might collapse into a
    control-plane dispatch."""
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN, mesh_key="x" * 32)
    for p in ("/api/memory/sync/../config/agents",
              "/api/memory/sync//../chat/sessions",
              "/api/memory/sync/%2e%2e/config/agents"):
        assert api._is_sync_path(p) is False, p
    assert api._is_sync_path("/api/memory/sync/manifest") is True
    assert api._is_sync_path("/api/memory/sync/blob/abc123") is True
