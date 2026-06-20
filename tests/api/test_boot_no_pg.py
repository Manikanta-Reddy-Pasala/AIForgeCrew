"""Phase-1 smoke: the API boots and serves tickets on SQLite, no Postgres."""
import importlib

import pytest

from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    store.create(title="seed ticket", assignee_role="doer", project="demo")

    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), store


def test_health_reports_sqlite_storage(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["storage"] == "sqlite"
    assert body["ok"] is True


def test_list_tickets_on_sqlite(client):
    c, _ = client
    r = c.get("/api/tickets")
    assert r.status_code == 200
    rows = r.json()
    assert any(t["title"] == "seed ticket" for t in rows)
    # enrichment keys present
    assert "active_role" in rows[0]
    assert "started_at" in rows[0]


def test_get_ticket_detail_on_sqlite(client):
    c, store = client
    ident = store.create(title="detail me", assignee_role="doer",
                         project="demo").identifier
    r = c.get(f"/api/tickets/{ident}")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"]["identifier"] == ident
    assert body["events"] == [] or isinstance(body["events"], list)
    assert isinstance(body["children"], list)


def test_agents_endpoint_does_not_500_on_sqlite(client):
    c, _ = client
    r = c.get("/api/agents")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
