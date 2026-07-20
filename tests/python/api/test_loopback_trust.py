"""Loopback trust is a configuration statement, and the bind guard reads reality.

Two findings from the adversarial review, both demonstrated end to end before
being fixed:

1. Behind a same-host reverse proxy (the documented Cloudflare → nginx → box
   deployment) ``request.client.host`` is ``127.0.0.1`` for every request on
   earth, so implicit loopback trust was a full auth bypass — ``/admin``,
   ``/api/admin/sync-status`` and the whole memory tree, with no token. Loopback
   trust now has to be declared (``AIFORGE_TRUST_LOOPBACK``, default on so a
   bare local run still works), and the admin surface never takes the shortcut.
2. ``_security_boot_guard`` compared the token against ``AIFORGE_BIND_HOST``,
   which only ``run.sh`` exports — so ``uvicorn --host 0.0.0.0`` with no token
   booted silently. It now inspects the real server.

``TestClient(app, client=(host, port))`` populates ``scope["client"]`` exactly
as a socket does, so the production branch runs unmocked.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "sh4red-t0ken"
LOOPBACK = ("127.0.0.1", 51000)
SYNC = "/api/memory/sync/manifest"
ADMIN_PATHS = ("/admin", "/api/admin/sync-status")


def _fresh_api(monkeypatch, tmp_path, *, token: str | None = TOKEN,
               trust: str | None = None):
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
    if trust is None:
        monkeypatch.delenv("AIFORGE_TRUST_LOOPBACK", raising=False)
    else:
        monkeypatch.setenv("AIFORGE_TRUST_LOOPBACK", trust)
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


# ── finding 1: loopback trust must be declared, and never covers /admin ─────

def test_trust_loopback_off_makes_a_local_caller_present_the_token(
        monkeypatch, tmp_path):
    """The proxy case: the peer IS 127.0.0.1 and it still gets 401."""
    api = _fresh_api(monkeypatch, tmp_path, trust="0")
    client = TestClient(api.app, client=LOOPBACK)

    assert client.get(SYNC).status_code == 401
    assert client.get(SYNC, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    # …and health stays open, so a fronted deployment is still probeable.
    assert client.get("/api/health").status_code == 200


def test_admin_needs_the_token_even_from_loopback(monkeypatch, tmp_path):
    """Highest-value surface, weakest signal: /admin does not accept the peer
    address as proof, even with loopback trust left at its default (on)."""
    api = _fresh_api(monkeypatch, tmp_path)
    client = TestClient(api.app, client=LOOPBACK)

    for path in ADMIN_PATHS:
        assert client.get(path).status_code == 401, path
        assert client.get(
            path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200, path


def test_loopback_stays_open_by_default_for_everything_else(monkeypatch, tmp_path):
    """The default must not break a bare local run: no AIFORGE_TRUST_LOOPBACK
    set, token configured, loopback peer → still allowed off the peer address."""
    api = _fresh_api(monkeypatch, tmp_path)
    assert TestClient(api.app, client=LOOPBACK).get(SYNC).status_code == 200


def test_admin_is_open_when_no_token_is_configured(monkeypatch, tmp_path):
    """No token configured → nothing to require; the loopback gate in
    routes.admin is still the one saying no to everyone else."""
    api = _fresh_api(monkeypatch, tmp_path, token=None)
    client = TestClient(api.app, client=LOOPBACK)
    for path in ADMIN_PATHS:
        assert client.get(path).status_code == 200, path


# ── finding 2: the guard must see the socket, not an env var ────────────────

def _run_uvicorn_startup(host: str) -> dict:
    """Boot a REAL uvicorn on ``host``, run the security guard from inside the
    startup hook exactly as the app does, and stop. Returns what it saw."""
    import uvicorn
    from fastapi import FastAPI

    import aiforge_core.api.api as api

    seen: dict = {}
    probe_app = FastAPI()

    @probe_app.on_event("startup")
    def _hook() -> None:
        seen["hosts"] = api._observed_bind_hosts()
        try:
            api._security_boot_guard()
        except RuntimeError as exc:
            seen["error"] = str(exc)
        server.should_exit = True

    server = uvicorn.Server(uvicorn.Config(probe_app, host=host, port=0,
                                           log_level="error"))
    server.run()
    return seen


def test_boot_guard_fires_for_a_real_wildcard_bind_with_no_token(
        monkeypatch, tmp_path):
    """``uvicorn --host 0.0.0.0`` with no token and no AIFORGE_BIND_HOST — the
    exact invocation that used to boot silently and publish a shell-running
    control plane to the LAN."""
    _fresh_api(monkeypatch, tmp_path, token=None)

    seen = _run_uvicorn_startup("0.0.0.0")

    assert seen["hosts"] == ["0.0.0.0"]          # observed, not read from env
    assert "non-loopback" in seen.get("error", "")


def test_boot_guard_stays_quiet_for_a_real_loopback_bind(monkeypatch, tmp_path):
    _fresh_api(monkeypatch, tmp_path, token=None)

    seen = _run_uvicorn_startup("127.0.0.1")

    assert seen["hosts"] == ["127.0.0.1"]
    assert "error" not in seen


def test_env_hint_is_only_a_fallback(monkeypatch, tmp_path):
    """Nothing observable (TestClient / unit test): the env hint is still used,
    so the existing contract holds — but it is a documented fallback now."""
    api = _fresh_api(monkeypatch, tmp_path, token=None)
    assert api._observed_bind_hosts() == []
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="non-loopback"):
        api._security_boot_guard()
    # An explicitly observed loopback socket OVERRIDES a scary-looking env var.
    api._security_boot_guard(hosts=["127.0.0.1"])
