"""Bearer-token API auth + non-loopback bind guard.

This control plane runs shell + edits files, so it must not be reachable
unauthenticated once exposed. Contract:
  * token SET  → protected /api route without token = 401, with token = 200
  * token UNSET + loopback (TestClient) = 200 (open — local dev / UI / tests)
  * non-loopback bind + no token → boot guard raises (refuse to expose)
Health stays open even with a token so the UI shell can load.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def _fresh_app(monkeypatch, tmp_path):
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
    return api


# A DB-free protected GET (middleware runs before routing, so 401 needs no DB;
# the 200 path returns a plain env-derived dict).
_PROTECTED = "/api/runtime/force_full_pipeline"


def test_token_set_requires_header(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cr3t")
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    api = _fresh_app(monkeypatch, tmp_path)
    client = TestClient(api.app)

    # No token → 401
    assert client.get(_PROTECTED).status_code == 401
    # Wrong token → 401
    assert client.get(_PROTECTED, headers={"Authorization": "Bearer nope"}).status_code == 401
    # Correct bearer token → 200
    r = client.get(_PROTECTED, headers={"Authorization": "Bearer s3cr3t"})
    assert r.status_code == 200
    # X-AIForge-Token header also accepted
    r2 = client.get(_PROTECTED, headers={"X-AIForge-Token": "s3cr3t"})
    assert r2.status_code == 200


def test_health_open_even_with_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cr3t")
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    api = _fresh_app(monkeypatch, tmp_path)
    client = TestClient(api.app)
    # Health must load without a token so the UI shell / probes work.
    assert client.get("/api/health").status_code == 200


def test_token_unset_loopback_is_open(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_BIND_HOST", raising=False)
    api = _fresh_app(monkeypatch, tmp_path)
    client = TestClient(api.app)
    # No token + loopback default → open (current local-dev behaviour).
    assert client.get(_PROTECTED).status_code == 200


def test_boot_guard_refuses_non_loopback_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", raising=False)
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    api = _fresh_app(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="non-loopback"):
        api._security_boot_guard()


def test_boot_guard_escape_hatch_allows_fronted_nonloopback(monkeypatch, tmp_path):
    # Operator fronts the api themselves (Cloudflare/WireGuard) → explicit
    # opt-out allows a non-loopback bind without a token.
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "1")
    api = _fresh_app(monkeypatch, tmp_path)
    api._security_boot_guard()   # must NOT raise


def test_boot_guard_allows_non_loopback_with_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_API_TOKEN", "s3cr3t")
    monkeypatch.setenv("AIFORGE_BIND_HOST", "0.0.0.0")
    api = _fresh_app(monkeypatch, tmp_path)
    # Token present → boot allowed on a non-loopback host.
    api._security_boot_guard()


def test_boot_guard_loopback_ok_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.setenv("AIFORGE_BIND_HOST", "127.0.0.1")
    api = _fresh_app(monkeypatch, tmp_path)
    api._security_boot_guard()


def test_cors_not_wildcard(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_API_TOKEN", raising=False)
    monkeypatch.delenv("AIFORGE_CORS_ORIGINS", raising=False)
    api = _fresh_app(monkeypatch, tmp_path)
    assert "*" not in api._cors_origins()
