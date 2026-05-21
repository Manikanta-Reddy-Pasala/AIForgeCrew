from __future__ import annotations

from unittest.mock import patch

import pytest

from aiforge_core.runtime.tools import mcp_client as mc


def test_list_endpoints_default():
    out = mc.mcp("list_endpoints")
    assert out["ok"]
    eps = out["endpoints"]
    assert "oneshell-mongo" in eps
    assert eps["oneshell-mongo"].startswith("http://")


def test_list_endpoints_env_override(monkeypatch):
    monkeypatch.setenv(
        "AIFORGE_MCP_ENDPOINTS",
        "my=http://x:8000, other=http://y:8001",
    )
    out = mc.list_endpoints()
    assert out["endpoints"] == {"my": "http://x:8000", "other": "http://y:8001"}


def test_list_tools_unknown_endpoint():
    out = mc.mcp("list_tools", endpoint="totally-fake")
    assert out["ok"] is False
    assert out["error"] == "unknown_endpoint"
    assert "oneshell-mongo" in out["allowed"]


def test_list_tools_happy(monkeypatch):
    def _fake(url, payload):
        assert payload["method"] == "tools/list"
        return {
            "jsonrpc": "2.0", "id": 1,
            "result": {"tools": [
                {"name": "find_business", "description": "lookup business"},
                {"name": "list_collections", "description": "mongo collections"},
            ]},
        }
    monkeypatch.setattr(mc, "_post_json", _fake)
    out = mc.list_tools("oneshell-mongo")
    assert out["ok"]
    assert len(out["tools"]) == 2
    assert out["tools"][0]["name"] == "find_business"


def test_list_tools_http_error(monkeypatch):
    import urllib.error
    def _boom(url, payload):
        raise urllib.error.HTTPError(url, 503, "down", {}, None)
    monkeypatch.setattr(mc, "_post_json", _boom)
    out = mc.list_tools("oneshell-mongo")
    assert out["ok"] is False
    assert out["error"] == "http_error"
    assert out["status"] == 503


def test_list_tools_connection_failure(monkeypatch):
    import urllib.error
    def _boom(url, payload):
        raise urllib.error.URLError("dns dead")
    monkeypatch.setattr(mc, "_post_json", _boom)
    out = mc.list_tools("oneshell-mongo")
    assert out["ok"] is False
    assert out["error"] == "connection_failed"


def test_call_tool_missing_tool():
    out = mc.mcp("call_tool", endpoint="oneshell-mongo", tool="")
    assert out["ok"] is False
    assert out["error"] == "missing_tool"


def test_call_tool_happy(monkeypatch):
    captured = {}
    def _fake(url, payload):
        captured["payload"] = payload
        return {
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "{'id': 'abc'}"}]},
        }
    monkeypatch.setattr(mc, "_post_json", _fake)
    out = mc.call_tool(
        "oneshell-mongo", "find_business",
        arguments={"business": "RHM"},
    )
    assert out["ok"]
    assert captured["payload"]["params"]["name"] == "find_business"
    assert captured["payload"]["params"]["arguments"] == {"business": "RHM"}
    assert "content" in out["result"]


def test_call_tool_mcp_error(monkeypatch):
    def _fake(url, payload):
        return {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32600, "message": "bad params"},
        }
    monkeypatch.setattr(mc, "_post_json", _fake)
    out = mc.call_tool("oneshell-mongo", "find_business")
    assert out["ok"] is False
    assert out["error"] == "mcp_error"
    assert out["detail"]["message"] == "bad params"


def test_dispatch_unknown_command():
    out = mc.mcp("teleport")
    assert out["ok"] is False
    assert out["error"] == "unknown_command"


def test_dispatch_list_tools_missing_endpoint():
    out = mc.mcp("list_tools")
    assert out["ok"] is False
    assert out["error"] == "missing_endpoint"
