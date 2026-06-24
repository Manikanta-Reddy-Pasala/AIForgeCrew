"""Confluence tool — REST shapes mocked at the urllib layer."""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime.tools import confluence as cf
from aiforge_core.runtime.tools import tool_policy


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://conf.internal")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "pat-123")
    monkeypatch.delenv("CONFLUENCE_USER", raising=False)


def _capture(monkeypatch, payload):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["body"] = json.loads(req.data.decode()) if req.data else None
        return _Resp(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def test_not_configured(monkeypatch):
    monkeypatch.delenv("CONFLUENCE_BASE_URL", raising=False)
    monkeypatch.delenv("CONFLUENCE_TOKEN", raising=False)
    assert cf.confluence_read({"id": "1"})["error"] == "confluence_not_configured"


def test_search(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"results": [
        {"id": "10", "title": "Runbook", "type": "page",
         "space": {"key": "ENG"}}]})
    out = cf.confluence_search({"query": "deploy"})
    assert out["ok"] and out["results"][0]["id"] == "10"
    assert "/rest/api/content/search" in seen["url"]
    assert "text" in seen["url"]                       # cql built from query
    assert seen["headers"].get("Authorization") == "Bearer pat-123"


def test_read_by_id(cfg, monkeypatch):
    _capture(monkeypatch, {"id": "10", "title": "Runbook",
                           "space": {"key": "ENG"},
                           "version": {"number": 4},
                           "body": {"storage": {"value": "<p>hi</p>"}}})
    out = cf.confluence_read({"id": "10"})
    assert out["ok"] and out["body"] == "<p>hi</p>" and out["version"] == 4


def test_create_requires_fields(cfg):
    assert cf.confluence_create({"title": "x"})["error"] == "missing 'space'"


def test_create(cfg, monkeypatch):
    seen = _capture(monkeypatch, {"id": "99", "title": "New",
                                  "_links": {"webui": "/display/ENG/New"}})
    out = cf.confluence_create({"title": "New", "space": "ENG",
                                "body": "<p>x</p>", "parent_id": "5"})
    assert out["ok"] and out["id"] == "99"
    assert seen["method"] == "POST"
    assert seen["body"]["space"]["key"] == "ENG"
    assert seen["body"]["ancestors"] == [{"id": "5"}]
    assert out["url"].endswith("/display/ENG/New")


def test_update_increments_version(cfg, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        if req.get_method() == "GET":
            return _Resp({"id": "10", "title": "Old", "version": {"number": 7}})
        # PUT
        assert json.loads(req.data.decode())["version"]["number"] == 8
        return _Resp({"id": "10", "_links": {"webui": "/x"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = cf.confluence_update({"id": "10", "body": "<p>new</p>"})
    assert out["ok"] and out["version"] == 8 and out["title"] == "Old"
    assert calls["n"] == 2


def test_basic_auth_when_user_set(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://conf.internal")
    monkeypatch.setenv("CONFLUENCE_TOKEN", "pw")
    monkeypatch.setenv("CONFLUENCE_USER", "alice")
    seen = _capture(monkeypatch, {"results": []})
    cf.confluence_search({"query": "x"})
    assert seen["headers"].get("Authorization", "").startswith("Basic ")


def test_writes_default_to_ask_policy(monkeypatch):
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_TOOL_POLICY", raising=False)
    assert tool_policy.decide("confluence_update", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("confluence_create", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("confluence_read", {})["policy"] == tool_policy.ALLOW
    # explicit override wins
    monkeypatch.setenv("AIFORGE_TOOL_POLICY", "confluence_update=allow")
    assert tool_policy.decide("confluence_update", {})["policy"] == tool_policy.ALLOW
