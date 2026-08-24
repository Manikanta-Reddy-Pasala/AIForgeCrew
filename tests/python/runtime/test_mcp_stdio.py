"""MCP stdio/npx transport — install + drive LOCAL MCP servers (Cursor/CC parity).

Covers the four contract points:
  (a) the registry no longer filters a stdio server out of the enabled set;
  (b) the client routes a stdio server config through the MCP SDK stdio path
      (with the right command/args) and returns the tool result dict;
  (c) the existing HTTP path is unchanged;
  (d) a missing SDK / command soft-fails to ``{ok: False, error}`` — never raises.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from aiforge_core.config import mcp_registry
from aiforge_core.runtime.tools import mcp_client as mc


# --------------------------------------------------------------------------- #
# (a) registry: stdio is installable + surfaces in the enabled stdio set       #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    return tmp_path


def test_catalog_has_installable_stdio_example(cfg):
    cat = mcp_registry.load_catalog()
    stdio = [c for c in cat if (c.get("transport") or "").lower() == "stdio"]
    assert stdio, "catalog should ship at least one local stdio example"
    # LOCAL-only: stdio examples must be a local subprocess, no url, no key.
    for c in stdio:
        assert c["installable"] is True
        assert c.get("command")
        assert not c.get("url")
        assert c.get("needs_api_key") is False


def test_stdio_server_not_filtered_from_enabled_set(cfg):
    row = mcp_registry.add_server(
        name="fs", transport="stdio", command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
    assert row["transport"] == "stdio"
    stdio = mcp_registry.enabled_stdio_servers()
    assert "fs" in stdio
    assert stdio["fs"]["command"] == "npx"
    assert stdio["fs"]["args"][0] == "-y"
    # HTTP endpoint map stays byte-identical: stdio never leaks into it.
    assert "fs" not in mcp_registry.enabled_endpoints()


def test_disabled_stdio_server_hidden(cfg):
    row = mcp_registry.add_server(
        name="fs", transport="stdio", command="npx", args=["x"])
    mcp_registry.update_server(row["id"], enabled=False)
    assert "fs" not in mcp_registry.enabled_stdio_servers()


def test_add_stdio_requires_command(cfg):
    with pytest.raises(ValueError):
        mcp_registry.add_server(name="bad", transport="stdio", command="")


def test_install_stdio_from_catalog(cfg):
    cat = mcp_registry.load_catalog()
    entry = next(c for c in cat if (c.get("transport") or "").lower() == "stdio")
    row = mcp_registry.install_from_catalog(entry["id"])
    assert row["transport"] == "stdio"
    assert row["command"]
    assert row["id"] in mcp_registry.enabled_stdio_servers() or \
        row["name"] in mcp_registry.enabled_stdio_servers()


# --------------------------------------------------------------------------- #
# Fakes for the async MCP SDK stdio surface                                    #
# --------------------------------------------------------------------------- #
class _FakeToolsResult:
    def __init__(self):
        self.tools = [
            SimpleNamespace(name="read_file", description="read a file"),
            SimpleNamespace(name="write_file", description="write a file"),
        ]


class _FakeCallResult:
    def __init__(self, name, arguments):
        self._name = name
        self._args = arguments
        self.isError = False

    def model_dump(self, mode=None):
        return {"content": [{"type": "text", "text": f"called {self._name}"}],
                "isError": False, "_args": self._args}


class _FakeSession:
    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolsResult()

    async def call_tool(self, name, arguments):
        return _FakeCallResult(name, arguments)


def _install_fake_sdk(monkeypatch, capture):
    class _FakeParams:
        def __init__(self, command, args=None, env=None, **kw):
            self.command = command
            self.args = args
            self.env = env

    @contextlib.asynccontextmanager
    async def _fake_stdio_client(params):
        capture["params"] = params
        yield ("READ", "WRITE")

    monkeypatch.setattr(mc, "_MCP_SDK_OK", True)
    monkeypatch.setattr(mc, "_StdioServerParameters", _FakeParams)
    monkeypatch.setattr(mc, "_stdio_client", _fake_stdio_client)
    monkeypatch.setattr(mc, "_ClientSession", _FakeSession)


_STDIO_CFG = {
    "fs": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"FOO": "bar"},
    }
}


# --------------------------------------------------------------------------- #
# (b) client routes a stdio server through the SDK stdio path                   #
# --------------------------------------------------------------------------- #
def test_client_lists_tools_over_stdio(monkeypatch):
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: dict(_STDIO_CFG))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture)

    out = mc.list_tools("fs")
    assert out["ok"] is True
    assert out["transport"] == "stdio"
    names = [t["name"] for t in out["tools"]]
    assert names == ["read_file", "write_file"]
    # spawned with the right command + args
    assert capture["params"].command == "npx"
    assert capture["params"].args == \
        ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_client_calls_tool_over_stdio(monkeypatch):
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: dict(_STDIO_CFG))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture)

    out = mc.call_tool("fs", "read_file", arguments={"path": "/tmp/a"})
    assert out["ok"] is True
    assert out["transport"] == "stdio"
    assert out["tool"] == "read_file"
    assert out["result"]["_args"] == {"path": "/tmp/a"}


def test_dispatch_routes_stdio(monkeypatch):
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: dict(_STDIO_CFG))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture)
    # stdio-only (no HTTP endpoints) must NOT trip the "no servers" guard.
    out = mc.mcp("list_tools", endpoint="fs")
    assert out["ok"] is True
    assert out["transport"] == "stdio"


# --------------------------------------------------------------------------- #
# (c) HTTP path unchanged                                                       #
# --------------------------------------------------------------------------- #
def test_http_path_unchanged(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "svc=http://svc:8000")
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: {})

    def _fake(url, payload):
        assert url == "http://svc:8000/mcp"
        assert payload["method"] == "tools/list"
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"tools": [{"name": "find_business", "description": "x"}]}}
    monkeypatch.setattr(mc, "_post_json", _fake)
    out = mc.list_tools("svc")
    assert out["ok"] is True
    assert out.get("transport") != "stdio"
    assert out["tools"][0]["name"] == "find_business"


# --------------------------------------------------------------------------- #
# (d) soft-fail: missing SDK / command → {ok: False, error}, never raises       #
# --------------------------------------------------------------------------- #
def test_stdio_softfail_when_sdk_missing(monkeypatch):
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: dict(_STDIO_CFG))
    monkeypatch.setattr(mc, "_MCP_SDK_OK", False)
    monkeypatch.setattr(mc, "_stdio_client", None)
    out = mc.list_tools("fs")
    assert out["ok"] is False
    assert out["error"]


def test_stdio_softfail_when_command_missing(monkeypatch):
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mc, "_load_stdio_servers", lambda: dict(_STDIO_CFG))
    capture: dict = {}
    _install_fake_sdk(monkeypatch, capture)

    @contextlib.asynccontextmanager
    async def _boom(params):
        raise FileNotFoundError("npx: command not found")
        yield  # pragma: no cover
    monkeypatch.setattr(mc, "_stdio_client", _boom)

    out = mc.call_tool("fs", "read_file")
    assert out["ok"] is False
    assert out["error"]
