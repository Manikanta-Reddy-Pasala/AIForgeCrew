"""Who may reach the hub sync endpoints, and who may not.

``/api/memory/sync/manifest`` enumerates what this machine advertises and
``/api/memory/sync/blob/{hash}`` hands back its bytes; ``/offer`` and ``/push``
let a spoke write. **By default all four answer without a credential** — the
admin is expected to sit on a trusted interface and the spokes are expected to
need no secret to keep in step. ``AIFORGE_SYNC_AUTH=1`` closes them again, and
then the ordinary API token is what a caller must present.

Contract exercised here (the middleware in ``api._require_token``):
  * sync routes, default          → open to everyone, token or no token
  * sync routes, SYNC_AUTH=1      → the API token, no exceptions
  * everything else               → unchanged: loopback is trusted, a remote
    caller needs the token, and forged X-Forwarded-For buys nothing
  * /api/health                   → open to everyone (UI shell + probes)
  * non-loopback bind, no token   → refused at boot

``TestClient(app, client=(host, port))`` populates ``scope["client"]`` exactly
as a real socket would, so the production branch runs — nothing is
monkeypatched, which is the point: a test that stubs the predicate cannot
notice the predicate being wrong.
"""
from __future__ import annotations

import base64
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
               sync_auth: str | None = None):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_BIND_HOST",
              "AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "AIFORGE_ADMIN_URL",
              "AIFORGE_ROLE"):
        monkeypatch.delenv(k, raising=False)
    if token is None:
        monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_API_TOKEN", token)
    if sync_auth is None:
        monkeypatch.delenv("AIFORGE_SYNC_AUTH", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_SYNC_AUTH", sync_auth)
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


def _seed_brief(tmp_path, text: str = "a merged fact") -> str:
    """The tier-1 merge is what an admin advertises downstream — see
    ``sync.inbox.downstream``. Raw captures and local briefs are never served."""
    body = (f'---\ntype: knowledge\nid: "M-01"\norigin: "book"\nrev: 2\n'
            f'updated_by: "book"\nderived: mesh\n---\n\n{text}\n')
    d = tmp_path / "md" / "mesh" / "book"
    d.mkdir(parents=True, exist_ok=True)
    (d / "M-01.md").write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode()).hexdigest()


# ── the default: sync is open, the control plane is not ────────────────────

def test_a_remote_spoke_reads_the_manifest_with_no_credential(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    digest = _seed_brief(tmp_path)

    r = TestClient(api.app, client=REMOTE).get(SYNC)

    assert r.status_code == 200
    assert [e["hash"] for e in r.json()["manifest"]] == [digest]


def test_a_remote_spoke_pushes_with_no_credential(monkeypatch, tmp_path):
    """The write half is open too — that is what "no auth for the admin" means.
    ``inbox`` is what bounds it: only what the spoke authored, never the fold."""
    api = _fresh_api(monkeypatch, tmp_path)
    client = TestClient(api.app, client=REMOTE)
    body = b"pushed from a spoke"
    entry = {"path": "captures/s-20260818-cccccc.md", "kind": "A",
             "hash": hashlib.sha256(body).hexdigest()}

    offer = client.post("/api/memory/sync/offer",
                        json={"peer": "studio", "entries": [entry]})
    assert offer.status_code == 200
    push = client.post("/api/memory/sync/push", json={
        "peer": "studio", "entry": entry, "body": base64.b64encode(body).decode()})
    assert push.status_code == 200 and push.json()["applied"] is True


def test_the_open_sync_surface_does_NOT_open_the_control_plane(monkeypatch, tmp_path):
    """The security crux: memory sync answering strangers must not mean the
    shell-running, config-writing routes do."""
    api = _fresh_api(monkeypatch, tmp_path)
    client = TestClient(api.app, client=REMOTE)

    for path in ("/api/config/agents", "/api/mcp/servers", "/api/chat/sessions",
                 "/api/tickets", "/api/repos"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} answered an unauthenticated remote"


def test_health_stays_open_for_remote_callers(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path)
    assert TestClient(api.app, client=REMOTE).get("/api/health").status_code == 200
    assert TestClient(api.app, client=LOOPBACK).get("/api/health").status_code == 200


# ── AIFORGE_SYNC_AUTH=1: the surface closes again ──────────────────────────

def test_sync_auth_requires_the_api_token_from_a_remote(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, sync_auth="1")
    digest = _seed_brief(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC).status_code == 401
    assert client.get(f"/api/memory/sync/blob/{digest}").status_code == 401

    ok = client.get(SYNC, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert client.get(f"/api/memory/sync/blob/{digest}",
                      headers={"X-AIForge-Token": TOKEN}).status_code == 200


def test_sync_auth_rejects_a_wrong_token(monkeypatch, tmp_path):
    api = _fresh_api(monkeypatch, tmp_path, sync_auth="1")
    _seed_brief(tmp_path)
    r = TestClient(api.app, client=REMOTE).get(
        SYNC, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_sync_auth_still_trusts_loopback(monkeypatch, tmp_path):
    """Closing the sync surface must not break the box's own tooling: anyone on
    this machine can read the memory tree off disk anyway."""
    api = _fresh_api(monkeypatch, tmp_path, sync_auth="1")
    _seed_brief(tmp_path)
    assert TestClient(api.app, client=LOOPBACK).get(SYNC).status_code == 200


def test_forged_loopback_headers_do_not_grant_access(monkeypatch, tmp_path):
    """The headline case: loopback is decided from the TCP peer only.

    Every header a reverse proxy would normally use to report the real client
    is settable by the client itself, so a remote attacker can claim
    127.0.0.1. There is no trusted proxy in front of this app.
    """
    api = _fresh_api(monkeypatch, tmp_path, sync_auth="1")
    digest = _seed_brief(tmp_path)
    client = TestClient(api.app, client=REMOTE)

    assert client.get(SYNC, headers=SPOOF_HEADERS).status_code == 401
    assert client.get(f"/api/memory/sync/blob/{digest}",
                      headers=SPOOF_HEADERS).status_code == 401
    # One header at a time, in case only one of them is consulted.
    for name, value in SPOOF_HEADERS.items():
        assert client.get(SYNC, headers={name: value}).status_code == 401, name


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


def test_boot_guard_does_not_refuse_an_open_sync_surface(monkeypatch, tmp_path):
    """It is the documented default, not a misconfiguration — the operator is
    told about it in the log, and the boot proceeds."""
    api = _fresh_api(monkeypatch, tmp_path, token=TOKEN)
    api._security_boot_guard(hosts=["127.0.0.1"])   # must not raise


# ── the sync path predicate ────────────────────────────────────────────────

def test_the_exemption_does_not_cover_a_traversal_path(monkeypatch, tmp_path):
    """`_is_sync_path` rejects dot-segments/encoded traversal, so the open sync
    surface cannot exempt a path a fronting proxy might collapse into a
    control-plane dispatch."""
    api = _fresh_api(monkeypatch, tmp_path)
    for p in ("/api/memory/sync/../config/agents",
              "/api/memory/sync//../chat/sessions",
              "/api/memory/sync/%2e%2e/config/agents"):
        assert api._is_sync_path(p) is False, p
    assert api._is_sync_path("/api/memory/sync/manifest") is True
    assert api._is_sync_path("/api/memory/sync/blob/abc123") is True


def test_a_traversal_path_is_not_reachable_unauthenticated(monkeypatch, tmp_path):
    """The predicate above, exercised through the middleware it guards."""
    api = _fresh_api(monkeypatch, tmp_path)
    client = TestClient(api.app, client=REMOTE)

    r = client.get("/api/memory/sync/../config/agents")
    assert r.status_code == 401
