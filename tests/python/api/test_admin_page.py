"""Loopback-only P2P sync admin surface (/admin + /api/admin/sync-status).

The headline tests are the gate ones: the admin surface must decide from the
TCP peer address alone, so a non-loopback client is refused even when it claims
``X-Forwarded-For: 127.0.0.1``. A non-loopback address is driven for real via
starlette's ``TestClient(client=(host, port))``, which sets ``scope["client"]``
exactly as a socket would — no monkeypatching of the gate itself.
"""
from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient

LOOPBACK = ("127.0.0.1", 40001)
REMOTE = ("10.0.0.9", 40002)


def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "sources.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)


def _seed_peers(tmp_path):
    """One approved peer (with a token, which must never be echoed) and one
    candidate, plus a capture so the manifest is non-empty."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "peers.json").write_text(json.dumps({
        "self": {"urls": ["http://127.0.0.1:8799"]},
        "peers": [
            {"id": "mac", "urls": ["http://10.0.0.5:8799"], "state": "approved",
             "last_seen": 1700000000, "token": "s3cr3t-token-value"},
            {"id": "laptop", "urls": ["http://10.0.0.6:8799"], "state": "candidate",
             "last_seen": 1700000001},
        ],
    }), encoding="utf-8")
    caps = tmp_path / "md" / "captures"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "a-20260719-aaaaaa.md").write_text("hello", encoding="utf-8")


def _fresh_api(monkeypatch, tmp_path):
    _isolate_env(monkeypatch, tmp_path)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.routes.admin as admin
    importlib.reload(admin)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from aiforge_core.runtime import chat_store
    chat_store.reset_backend_for_tests()
    return api, admin


def _no_probe(monkeypatch, admin, calls):
    monkeypatch.setattr(admin, "_probe_all",
                        lambda peers: calls.append(peers) or [{} for _ in peers])


# ────────────────────────────── JSON shape ──────────────────────────────────

def test_sync_status_shape(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    _no_probe(monkeypatch, admin, [])
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status").json()
    assert body["self"]["id"] == "nuc"
    assert body["self"]["urls"] == ["http://127.0.0.1:8799"]
    assert set(body["lease"]) == {"holder", "is_holder", "expires_in", "rev"}
    assert body["lease"]["is_holder"] is False
    assert body["local"]["class_a"] == 1
    assert set(body["local"]) == {"class_a", "class_b", "tombstones", "conflicts", "total"}
    assert body["probed"] is True

    ids = [p["id"] for p in body["peers"]]
    assert ids == ["mac", "laptop"]
    mac = body["peers"][0]
    assert mac["state"] == "approved"
    assert mac["urls"] == ["http://10.0.0.5:8799"]
    assert mac["last_seen"] == 1700000000
    assert set(mac) >= {"reachable", "latency_ms", "their_entries", "error"}
    assert body["peers"][1]["state"] == "candidate"


def test_tokens_never_appear_in_json(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    _no_probe(monkeypatch, admin, [])
    c = TestClient(api.app, client=LOOPBACK)
    raw = c.get("/api/admin/sync-status").text
    assert "s3cr3t-token-value" not in raw
    assert "token" not in raw


# ──────────────────────────── the loopback gate ─────────────────────────────

def test_non_loopback_client_is_403_on_both_surfaces(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    c = TestClient(api.app, client=REMOTE)
    for path in ("/admin", "/api/admin/sync-status"):
        r = c.get(path)
        assert r.status_code == 403, path
        assert "ssh" in r.text.lower()          # tells the operator how to get in


def test_spoofed_forwarded_headers_do_not_bypass_the_gate(monkeypatch, tmp_path):
    """The headline security test: headers are attacker-controlled, so only the
    real peer address may decide."""
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    c = TestClient(api.app, client=REMOTE)
    spoof = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1",
             "Host": "127.0.0.1:8799", "Forwarded": "for=127.0.0.1"}
    for path in ("/admin", "/api/admin/sync-status"):
        assert c.get(path, headers=spoof).status_code == 403, path


def test_missing_client_is_rejected(monkeypatch, tmp_path):
    """A scope with no client at all (some ASGI servers) must not read as local."""
    import pytest
    from fastapi import HTTPException

    _api, admin = _fresh_api(monkeypatch, tmp_path)

    class _Req:
        client = None

    with pytest.raises(HTTPException) as exc:
        admin._require_loopback(_Req())
    assert exc.value.status_code == 403


def test_ipv6_loopback_forms_are_accepted(monkeypatch, tmp_path):
    _api, admin = _fresh_api(monkeypatch, tmp_path)

    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        class _Req:
            client = type("C", (), {"host": host})()

        admin._require_loopback(_Req())   # no raise


# ───────────────────────────────── probing ──────────────────────────────────

def test_probe_0_performs_no_network_calls(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)

    called: list = []
    monkeypatch.setattr(admin, "_probe_all", lambda peers: called.append(peers) or [])
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status?probe=0").json()
    assert called == []                       # the probe was never entered
    assert body["probed"] is False
    assert all(p["reachable"] is False and p["latency_ms"] is None for p in body["peers"])


def test_unreachable_peer_reports_error_without_failing_the_request(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)

    import httpx

    def _boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _boom)
    c = TestClient(api.app, client=LOOPBACK)

    r = c.get("/api/admin/sync-status")
    assert r.status_code == 200
    for p in r.json()["peers"]:
        assert p["reachable"] is False
        assert "connection refused" in p["error"]
        assert p["their_entries"] is None


def test_probe_counts_peer_manifest_entries(monkeypatch, tmp_path):
    _api, admin = _fresh_api(monkeypatch, tmp_path)
    import httpx

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"manifest": [{"path": "a"}, {"path": "b"}], "roster": []}

    seen: dict = {}

    def _ok(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers")
        seen["timeout"] = kw.get("timeout")
        return _Resp()

    monkeypatch.setattr(httpx, "get", _ok)
    res = admin._probe_peer({"id": "mac", "urls": ["http://10.0.0.5:8799"], "token": "tk"})
    assert res["reachable"] is True
    assert res["their_entries"] == 2
    assert seen["url"] == "http://10.0.0.5:8799/api/memory/sync/manifest"
    assert seen["headers"] == {"Authorization": "Bearer tk"}
    assert seen["timeout"] == admin.PROBE_TIMEOUT      # short — this runs in a page load


def test_peer_without_url_is_not_probed(monkeypatch, tmp_path):
    _api, admin = _fresh_api(monkeypatch, tmp_path)
    res = admin._probe_peer({"id": "ghost", "urls": []})
    assert res["reachable"] is False
    assert "no url" in res["error"]


# ─────────────────────────────── the page ───────────────────────────────────

def test_admin_page_renders_for_loopback(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    c = TestClient(api.app, client=LOOPBACK)

    r = c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "sync-status" in r.text          # the page fetches its own data
    assert "candidate" in r.text            # and teaches the trust distinction


def test_page_embeds_the_peer_ids_and_no_token(monkeypatch, tmp_path):
    """The boot payload is the unprobed status, so the peer ids are in the HTML
    itself — and, being the same shaping code, still carry no token."""
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)

    called: list = []
    monkeypatch.setattr(admin, "_probe_all", lambda peers: called.append(peers) or [])
    c = TestClient(api.app, client=LOOPBACK)

    html = c.get("/admin").text
    assert '"mac"' in html and '"laptop"' in html
    assert "s3cr3t-token-value" not in html
    assert called == []          # rendering the page makes no network calls
