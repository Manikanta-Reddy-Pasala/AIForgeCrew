"""The admin "Allow all outbound hosts" switch."""
import json
import os

import pytest


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for k in ("AIFORGE_EGRESS_ALLOW_ALL", "AIFORGE_EGRESS_POLICY_RO", "AIFORGE_EGRESS_OFF",
              "AIFORGE_ALLOW_WEB_FETCH", "AIFORGE_EGRESS_ALLOW_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_switch_opens_every_host_and_web_fetch(cfg):
    from aiforge_core.config import egress_hosts as eh
    from aiforge_core.net import egress
    assert not egress.host_allowed("https://example.org/x")
    assert not egress.fetch_allowed()
    eh.set_stored_hosts(["docs.python.org"])
    eh.set_allow_all(True)
    assert egress.host_allowed("https://example.org/x") and egress.fetch_allowed()
    assert eh.stored_hosts() == ["docs.python.org"]                  # list kept
    assert eh.describe()["allow_all"] is True
    eh.set_allow_all(False)
    assert not egress.host_allowed("https://example.org/x")
    assert egress.host_allowed("https://docs.python.org/3/")


def test_env_and_network_policy_and_master_off(cfg, monkeypatch, tmp_path):
    from aiforge_core.config import egress_hosts as eh
    from aiforge_core.net import egress
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_ALL", "1")
    assert egress.host_allowed("https://any.example.net")
    monkeypatch.setenv("AIFORGE_EGRESS_OFF", "1")
    assert not egress.fetch_allowed()                                # master off wins
    monkeypatch.delenv("AIFORGE_EGRESS_OFF")
    monkeypatch.delenv("AIFORGE_EGRESS_ALLOW_ALL")
    pol = tmp_path / "pol"
    pol.mkdir()
    (pol / "hosts.json").write_text(json.dumps({"hosts": ["pypi.org"], "allow_all": True}))
    monkeypatch.setenv("AIFORGE_EGRESS_POLICY_RO", str(pol))
    assert eh.network_policy()["allow_all"] is True and eh.allow_all()


def test_routes_take_the_switch(cfg):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aiforge_core.api.routes import runtime_net
    app = FastAPI(); app.include_router(runtime_net.router)
    c = TestClient(app)
    r = c.put("/api/runtime/egress_hosts", json={"extra_hosts": ["a.example.com"], "allow_all": True}).json()
    assert r["allow_all"] is True and r["extra_hosts"] == ["a.example.com"]
    r = c.put("/api/runtime/egress_hosts", json={"extra_hosts": ["a.example.com"]}).json()
    assert r["allow_all"] is True                                    # unchanged when omitted
    r = c.put("/api/runtime/egress_hosts", json={"extra_hosts": [], "allow_all": False}).json()
    assert r["allow_all"] is False
