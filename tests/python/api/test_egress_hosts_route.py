"""The Settings surface for the egress allowlist.

Tested through the ROUTE, not the store: the store-level behaviour is covered
in tests/python/config/test_egress_hosts.py, and this repo has been bitten
before by a validator that accepted a value the runtime then rejected (the
chat_safety_cap `ge=1` vs a documented 0). What matters here is that what the
screen sends is what the gate ends up enforcing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiforge_core.api.api import app


@pytest.fixture(autouse=True)
def _no_stale_derivation():
    """The derived host set is memoized for a few seconds (it touches ~20 files
    and runs on every outbound decision). A test that sets an env var and asks
    immediately would otherwise read the previous answer."""
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    yield
    _eh._invalidate()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_EGRESS_ALLOW_HOSTS", raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def test_get_reports_derived_entries_not_just_stored(client, monkeypatch):
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://model.corp.example/v1")
    body = client.get("/api/runtime/egress_hosts").json()
    assert "model.corp.example" in body["derived"]
    assert "model.corp.example" in body["effective"]


def test_saving_a_host_actually_opens_the_gate(client):
    """The point of the screen. Asserted against the GATE, because a settings
    round-trip that the enforcement layer never reads is the failure mode."""
    from aiforge_core.net import egress

    assert egress.host_allowed("https://docs.python.org/3/") is False
    r = client.put("/api/runtime/egress_hosts",
                   json={"extra_hosts": ["https://docs.python.org/3/library/"]})
    assert r.status_code == 200
    assert r.json()["extra_hosts"] == ["docs.python.org"]
    assert egress.host_allowed("https://docs.python.org/3/") is True


def test_removing_it_closes_the_gate_again(client):
    from aiforge_core.net import egress

    client.put("/api/runtime/egress_hosts", json={"extra_hosts": ["a.example"]})
    assert egress.host_allowed("https://a.example/x") is True
    client.put("/api/runtime/egress_hosts", json={"extra_hosts": []})
    assert egress.host_allowed("https://a.example/x") is False


def test_a_pasted_url_is_stored_as_its_host(client):
    r = client.put("/api/runtime/egress_hosts",
                   json={"extra_hosts": ["https://a.example:8443/path?q=1"]})
    assert r.json()["extra_hosts"] == ["a.example"]


@pytest.mark.parametrize("body,why", [
    ({"extra_hosts": ["x" * 300]}, "longer than a DNS name can be"),
    ({"extra_hosts": [f"h{i}.example" for i in range(101)]}, "over the count cap"),
])
def test_the_route_refuses_junk(client, body, why):
    assert client.put("/api/runtime/egress_hosts", json=body).status_code == 400, why


def test_an_empty_body_is_a_valid_clear(client):
    assert client.put("/api/runtime/egress_hosts", json={}).status_code == 200
