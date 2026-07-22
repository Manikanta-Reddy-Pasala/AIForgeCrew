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
    # The elected leader, not a lease record. 'mac' was last reached in 2023,
    # i.e. far outside the alive window, so it is not even a candidate and we
    # lead by default.
    assert set(body["leader"]) == {"leader", "is_us"}
    assert body["leader"] == {"leader": "nuc", "is_us": True}
    assert body["local"]["class_a"] == 1
    assert set(body["local"]) == {"class_a", "class_b", "tombstones", "conflicts",
                                  "total", "okf", "peers", "mesh", "view"}
    assert body["probed"] is True

    ids = [p["id"] for p in body["peers"]]
    assert ids == ["mac", "laptop"]
    mac = body["peers"][0]
    assert mac["state"] == "approved"
    assert mac["urls"] == ["http://10.0.0.5:8799"]
    assert mac["last_seen"] == 1700000000
    assert set(mac) >= {"reachable", "latency_ms", "their_entries", "error"}
    assert body["peers"][1]["state"] == "candidate"


def test_local_counts_say_which_directory_holds_what(monkeypatch, tmp_path):
    """Each of the four directories has a different writer, so one total tells
    you nothing about where knowledge actually is."""
    api, admin = _fresh_api(monkeypatch, tmp_path)
    _seed_peers(tmp_path)
    _no_probe(monkeypatch, admin, [])
    seq = iter(range(99))
    for folder, count in (("okf", 2), ("peers", 3), ("mesh", 1), ("view", 4)):
        d = tmp_path / "md" / folder / "nuc"
        d.mkdir(parents=True, exist_ok=True)
        for _ in range(count):
            key = f"L-{next(seq):02d}"          # distinct identities, not copies
            (d / f"{key}.md").write_text(
                f'---\ntype: learning\nid: "{key}"\norigin: "nuc"\nrev: 1\n'
                f'updated_by: "nuc"\n---\n\nb\n', encoding="utf-8")
    c = TestClient(api.app, client=LOOPBACK)

    local = c.get("/api/admin/sync-status").json()["local"]

    assert (local["okf"], local["peers"], local["mesh"], local["view"]) == (2, 3, 1, 4)
    # view/ is local-only, so it is counted but never advertised: 2+3+1 class B.
    assert local["class_b"] == 6


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
    # 'mac' is approved, so it is probed and its failure is reported; 'laptop'
    # is a candidate and is never contacted at all (see the SSRF tests below).
    mac, laptop = r.json()["peers"]
    assert mac["reachable"] is False and mac["probed"] is True
    assert "connection refused" in mac["error"]
    assert mac["their_entries"] is None
    assert laptop["probed"] is False and laptop["error"] is None


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


def test_candidate_peers_are_listed_but_never_probed(monkeypatch, tmp_path):
    """A candidate's url arrived over gossip or SSDP — unauthenticated channels.
    Probing one makes an operator's page load fetch whatever a stranger named
    (an internal service, say) and renders reachability, latency and the
    exception text back: a port scanner driven through peer discovery.
    """
    _api, admin = _fresh_api(monkeypatch, tmp_path)
    probed: list = []
    monkeypatch.setattr(admin, "_probe_peer",
                        lambda p: probed.append(p["id"]) or {"reachable": True,
                                                             "probed": True})

    roster = [{"id": "mac", "state": "approved", "urls": ["http://10.0.0.5:8799"]},
              {"id": "evil", "state": "candidate", "urls": ["http://10.0.0.1:8080"]},
              {"id": "vague", "state": "", "urls": ["http://169.254.169.254/"]}]
    out = admin._probe_all(roster)

    assert probed == ["mac"]                      # and nothing else was touched
    assert out[0]["reachable"] is True and out[0]["probed"] is True
    for skipped in out[1:]:
        assert skipped == {"reachable": False, "latency_ms": None,
                           "their_entries": None, "error": None, "probed": False}


def test_probe_fanout_and_budget_bound_one_page_load(monkeypatch, tmp_path):
    """A hostile roster is unbounded (gossip grows peers.json), so the page must
    be bounded: at most PROBE_MAX_PEERS contacted, and never longer than
    PROBE_BUDGET, whatever the peers do."""
    import time

    _api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setattr(admin, "PROBE_BUDGET", 0.2)
    calls: list = []

    def _blackhole(peer):
        calls.append(peer["id"])
        time.sleep(1.0)                            # never answers in budget
        return {"reachable": True, "probed": True}

    monkeypatch.setattr(admin, "_probe_peer", _blackhole)
    roster = [{"id": f"p{i}", "state": "approved", "urls": ["http://10.0.0.9:9"]}
              for i in range(200)]

    started = time.monotonic()
    out = admin._probe_all(roster)
    elapsed = time.monotonic() - started

    assert elapsed < 0.9                           # returned on the budget, not on the peers
    assert len(calls) <= admin.PROBE_MAX_PEERS     # fan-out capped
    assert len(out) == 200                         # every peer still has its row
    assert out[0]["error"] == "probe budget exceeded"
    assert all(p["reachable"] is False for p in out[admin.PROBE_MAX_PEERS:])


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


# ─────────────────────── add peer by IP (seed) ──────────────────────────────
#
# The manual fallback for when SSDP can't reach a peer. It must: be loopback-
# only; refuse to leak the mesh key to a typo'd address (challenge FIRST, key
# never sent); and only approve a peer that proves the shared key.

import hashlib
import hmac as _hmac


def _stub_seed_transport(monkeypatch, *, server_key, self_id="mac",
                         url="http://10.0.0.5:8799"):
    """Stand in for the peer at `url`: it answers the challenge with
    HMAC(server_key, nonce) and serves a roster advertising `self_id`. A
    `server_key` of None means the peer never answers (unreachable / no key)."""
    sent = {"manifest_token": None}

    def _proof(base_url, nonce):
        if server_key is None:
            return ""
        return _hmac.new(server_key.encode(), nonce.encode(),
                         hashlib.sha256).hexdigest()

    def _manifest(base_url, token=""):
        sent["manifest_token"] = token
        return {"manifest": [], "roster": [{"id": self_id, "urls": [url]}]}

    monkeypatch.setattr("aiforge_core.memory.sync.transport.membership_proof", _proof)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _manifest)
    return sent


def _peer_state(tmp_path, pid):
    import json as _json
    f = tmp_path / "cfg" / "peers.json"
    if not f.exists():
        return None
    for p in _json.loads(f.read_text()).get("peers", []):
        if p.get("id") == pid:
            return p.get("state")
    return None


def test_add_peer_by_ip_is_loopback_only(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_MESH_KEY", "x" * 32)
    r = TestClient(api.app, client=REMOTE).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799"})
    assert r.status_code == 403


def test_add_peer_that_proves_the_key_is_approved(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_MESH_KEY", "shared" * 6)
    sent = _stub_seed_transport(monkeypatch, server_key="shared" * 6)
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799/"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "mac" and body["state"] == "approved"
    assert _peer_state(tmp_path, "mac") == "approved"
    # The mesh key WAS sent to fetch the manifest — but only AFTER the challenge
    # proved membership; that ordering is asserted by the wrong-key test below.
    assert sent["manifest_token"] == "shared" * 6


def test_add_peer_with_a_wrong_key_is_refused_and_never_sent_the_key(monkeypatch,
                                                                     tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_MESH_KEY", "ours" * 8)
    # The peer holds a DIFFERENT key, so the challenge fails.
    sent = _stub_seed_transport(monkeypatch, server_key="theirs" * 6, self_id="evil")
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.9:8799"})
    assert r.status_code == 502
    # The key was NEVER sent: fetch_manifest is only reached after a good
    # challenge, which never happened.
    assert sent["manifest_token"] is None
    assert _peer_state(tmp_path, "evil") is None      # not added


def test_add_peer_unreachable_is_502(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_MESH_KEY", "k" * 32)
    _stub_seed_transport(monkeypatch, server_key=None)   # never answers
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799"})
    assert r.status_code == 502


def test_add_peer_zero_config_unauthenticated_peer_is_approved(monkeypatch, tmp_path):
    # No mesh key, no token: on a token-less mesh the peer serves its manifest
    # unauthenticated, so an IP alone is enough — no key/token to enter.
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.delenv("AIFORGE_MESH_KEY", raising=False)
    sent = _stub_seed_transport(monkeypatch, server_key=None)   # no challenge
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799/"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "mac" and body["state"] == "approved"
    assert _peer_state(tmp_path, "mac") == "approved"
    assert sent["manifest_token"] == ""           # fetched UNauthenticated


def test_add_peer_zero_config_protected_peer_is_502_with_guidance(monkeypatch, tmp_path):
    # No key/token AND the peer is token-protected → no manifest for an empty
    # token → a 502 that tells the operator how to authorise (no leak).
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.delenv("AIFORGE_MESH_KEY", raising=False)

    def _protected_manifest(base_url, token=""):
        return {} if not token else {"roster": [{"id": "mac", "urls": [base_url]}]}
    monkeypatch.setattr(
        "aiforge_core.memory.sync.transport.fetch_manifest", _protected_manifest)
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "AIFORGE_MESH_KEY" in detail and "Token" in detail


def test_add_peer_with_a_token_stores_it_as_the_peer_bearer(monkeypatch, tmp_path):
    # No mesh key, but a shared token supplied → authenticate with it, approve,
    # and persist it as the peer's bearer so the sync loop can auth with no env.
    import json as _json
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.delenv("AIFORGE_MESH_KEY", raising=False)
    sent = _stub_seed_transport(monkeypatch, server_key=None)
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "http://10.0.0.5:8799", "token": "sekret"})
    assert r.status_code == 200, r.text
    assert sent["manifest_token"] == "sekret"     # authed with the supplied token
    peers = _json.loads((tmp_path / "cfg" / "peers.json").read_text())["peers"]
    row = next(p for p in peers if p.get("id") == "mac")
    assert row.get("token") == "sekret" and row.get("state") == "approved"


def test_add_peer_rejects_a_non_http_url(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_MESH_KEY", "k" * 32)
    r = TestClient(api.app, client=LOOPBACK).post(
        "/api/admin/peers", json={"url": "ssh://10.0.0.5"})
    assert r.status_code == 400
