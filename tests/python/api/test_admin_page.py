"""Loopback-only hub sync admin surface (/admin + /api/admin/sync-status).

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


def _isolate_env(monkeypatch, tmp_path, *, admin_url: str = ""):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    monkeypatch.delenv("AIFORGE_ADMIN_ID", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_CHAT_DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "sources.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    if admin_url:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", admin_url)
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)


def _seed(tmp_path):
    """A capture so the manifest is non-empty, and two spokes on the roll."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "spokes.json").write_text(json.dumps({
        "spokes": {"mac": 1700000000, "laptop": 1700000001}}), encoding="utf-8")
    caps = tmp_path / "md" / "captures"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / "a-20260719-aaaaaa.md").write_text("hello", encoding="utf-8")


def _fresh_api(monkeypatch, tmp_path, *, admin_url: str = ""):
    _isolate_env(monkeypatch, tmp_path, admin_url=admin_url)
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


# ────────────────────────────── JSON shape ──────────────────────────────────

def test_sync_status_shape_on_the_admin(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)      # no admin url ⇒ we are it
    _seed(tmp_path)
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status").json()
    assert body["self"]["id"] == "nuc"
    # Configuration, not an election: the role answers immediately and names
    # itself as the admin.
    assert set(body["role"]) == {"role", "is_admin", "admin_url", "admin_id",
                                 "self", "stranded", "stale_url"}
    assert body["role"]["role"] == "admin"
    assert body["role"]["is_admin"] is True
    assert body["role"]["admin_id"] == "nuc"
    assert body["local"]["class_a"] == 1
    assert set(body["local"]) == {"class_a", "class_b", "tombstones", "conflicts",
                                  "total", "okf", "peers", "mesh", "view"}
    assert [s["id"] for s in body["spokes"]] == ["laptop", "mac"]   # newest first
    # An admin has nothing upstream to probe, so the probe is never entered.
    assert body["admin"]["probed"] is False


def test_a_spoke_with_no_admin_named_is_reported_as_stranded(monkeypatch, tmp_path):
    """It neither syncs (nowhere to sync to) nor merges (not the admin). The
    page used to render that state as "merges only its own knowledge"."""
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    monkeypatch.setenv("AIFORGE_ROLE", "spoke")
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status").json()

    assert body["role"]["stranded"] is True
    assert "neither syncs nor merges" in c.get("/admin").text


def test_an_admin_carrying_a_stale_url_shows_it(monkeypatch, tmp_path):
    """Otherwise the page renders "admin: this machine" and hides the very
    setting that would make a restart demote the box."""
    api, _admin = _fresh_api(monkeypatch, tmp_path,
                             admin_url="http://someone-else:8799")
    monkeypatch.setenv("AIFORGE_ROLE", "admin")
    c = TestClient(api.app, client=LOOPBACK)

    role = c.get("/api/admin/sync-status").json()["role"]

    assert role["is_admin"] is True
    assert role["stale_url"] == "http://someone-else:8799"


def test_sync_status_shape_on_a_spoke(monkeypatch, tmp_path):
    api, admin = _fresh_api(monkeypatch, tmp_path, admin_url="http://10.0.0.5:8799")
    _seed(tmp_path)
    monkeypatch.setattr(admin, "_probe_admin",
                        lambda url: {"reachable": True, "latency_ms": 4,
                                     "entries": 7, "error": None, "probed": True})
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status").json()
    assert body["role"]["role"] == "spoke"
    assert body["role"]["is_admin"] is False
    assert body["role"]["admin_url"] == "http://10.0.0.5:8799"
    assert body["admin"]["reachable"] is True and body["admin"]["entries"] == 7
    # The roll is the admin's bookkeeping; a spoke never shows one.
    assert body["spokes"] == []


def test_local_counts_say_which_directory_holds_what(monkeypatch, tmp_path):
    """Each of the four directories has a different writer, so one total tells
    you nothing about where knowledge actually is."""
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed(tmp_path)
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


# ──────────────────────────── the loopback gate ─────────────────────────────

def test_non_loopback_client_is_403_on_both_surfaces(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed(tmp_path)
    c = TestClient(api.app, client=REMOTE)
    for path in ("/admin", "/api/admin/sync-status"):
        r = c.get(path)
        assert r.status_code == 403, path
        assert "ssh" in r.text.lower()          # tells the operator how to get in


def test_spoofed_forwarded_headers_do_not_bypass_the_gate(monkeypatch, tmp_path):
    """The headline security test: headers are attacker-controlled, so only the
    real peer address may decide."""
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed(tmp_path)
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
    api, admin = _fresh_api(monkeypatch, tmp_path, admin_url="http://10.0.0.5:8799")
    _seed(tmp_path)

    called: list = []
    monkeypatch.setattr(admin, "_probe_admin", lambda url: called.append(url) or {})
    c = TestClient(api.app, client=LOOPBACK)

    body = c.get("/api/admin/sync-status?probe=0").json()
    assert called == []                       # the probe was never entered
    assert body["probed"] is False
    assert body["admin"]["reachable"] is False and body["admin"]["latency_ms"] is None


def test_an_unreachable_admin_reports_the_error_without_failing_the_request(
        monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path, admin_url="http://10.0.0.5:8799")
    _seed(tmp_path)

    import httpx

    def _boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _boom)
    c = TestClient(api.app, client=LOOPBACK)

    r = c.get("/api/admin/sync-status")
    assert r.status_code == 200
    up = r.json()["admin"]
    assert up["reachable"] is False and up["probed"] is True
    assert "connection refused" in up["error"]
    assert up["entries"] is None


def test_the_probe_counts_the_admins_advertised_entries(monkeypatch, tmp_path):
    _api, admin = _fresh_api(monkeypatch, tmp_path)
    import httpx

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"manifest": [{"path": "a"}, {"path": "b"}], "admin": "hub"}

    seen: dict = {}

    def _ok(url, **kw):
        seen["url"] = url
        seen["timeout"] = kw.get("timeout")
        return _Resp()

    monkeypatch.setattr(httpx, "get", _ok)
    res = admin._probe_admin("http://10.0.0.5:8799")
    assert res["reachable"] is True
    assert res["entries"] == 2
    assert res["id"] == "hub"
    assert seen["url"] == "http://10.0.0.5:8799/api/memory/sync/manifest"
    # No credential is sent: the hub sync surface takes none by default, and a
    # page load must not be where one leaks.
    assert seen["timeout"] == admin.PROBE_TIMEOUT      # short — this is a page load


def test_an_admin_with_no_url_is_not_probed(monkeypatch, tmp_path):
    _api, admin = _fresh_api(monkeypatch, tmp_path)
    res = admin._probe_admin("")
    assert res["reachable"] is False and res["probed"] is False


# ─────────────────────────────── the page ───────────────────────────────────

def test_admin_page_renders_for_loopback(monkeypatch, tmp_path):
    api, _admin = _fresh_api(monkeypatch, tmp_path)
    _seed(tmp_path)
    c = TestClient(api.app, client=LOOPBACK)

    r = c.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "sync-status" in r.text          # the page fetches its own data
    assert "AIFORGE_ADMIN_URL" in r.text    # and says how to point a spoke here


def test_the_page_embeds_local_state_and_makes_no_network_call(monkeypatch, tmp_path):
    """The boot payload is the unprobed status, so the page paints immediately."""
    api, admin = _fresh_api(monkeypatch, tmp_path, admin_url="http://10.0.0.5:8799")
    _seed(tmp_path)

    called: list = []
    monkeypatch.setattr(admin, "_probe_admin", lambda url: called.append(url) or {})
    c = TestClient(api.app, client=LOOPBACK)

    html = c.get("/admin").text
    assert '"nuc"' in html
    assert "10.0.0.5" in html
    assert called == []          # rendering the page makes no network calls
