"""Talking to MCP servers over both transports.

Two kinds of server sit behind one tool: HTTP ones (JSON-RPC to ``/mcp``) and
LOCAL stdio ones spawned from the marketplace registry. The routing is by the
server's declared transport, and everything about the plumbing is soft-fail —
an MCP server is third-party, and a broken one must never take the agent loop
down with it.

Two specific hazards are pinned here. A stdio endpoint string ("npx server-x")
handed to urllib raises ValueError("unknown url type"), which no caller
catches — so URLs are policy-checked and refused as OSError, which the soft
handlers already cover. And the MCP SDK is async while this tool is sync, so
the bridge has to work even when it is called from inside a running event
loop, where asyncio.run() refuses.
"""
from __future__ import annotations

import asyncio
import json
import types as pytypes
import urllib.error

import pytest

from aiforge_core.runtime.tools import mcp_client as M


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    """No marketplace servers unless a test adds them."""
    from aiforge_core.config import mcp_registry
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    monkeypatch.setattr(mcp_registry, "enabled_endpoints", lambda: {})
    monkeypatch.setattr(mcp_registry, "enabled_stdio_servers", lambda: {})


# ─── where the servers come from ───────────────────────────────────────


def test_endpoints_come_from_the_env_csv(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "a=http://a:1, b=http://b:2")
    assert M._load_endpoints() == {"a": "http://a:1", "b": "http://b:2"}


def test_a_malformed_env_entry_is_skipped(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "nonsense,,a=http://a:1")
    assert M._load_endpoints() == {"a": "http://a:1"}


def test_the_marketplace_choice_wins_over_the_env(monkeypatch):
    """The registry row is the explicit choice the user made in the UI."""
    from aiforge_core.config import mcp_registry
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "a=http://env:1")
    monkeypatch.setattr(mcp_registry, "enabled_endpoints",
                        lambda: {"a": "http://ui:2"})
    assert M._load_endpoints()["a"] == "http://ui:2"


def test_a_broken_registry_never_hides_the_env_servers(monkeypatch):
    from aiforge_core.config import mcp_registry
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "a=http://a:1")
    monkeypatch.setattr(mcp_registry, "enabled_endpoints",
                        lambda: (_ for _ in ()).throw(OSError("cfg")))
    monkeypatch.setattr(mcp_registry, "enabled_stdio_servers",
                        lambda: (_ for _ in ()).throw(OSError("cfg")))
    assert M._load_endpoints() == {"a": "http://a:1"}
    assert M._load_stdio_servers() == {}


def test_both_transports_appear_in_one_map(monkeypatch):
    from aiforge_core.config import mcp_registry
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "web=http://a:1")
    monkeypatch.setattr(mcp_registry, "enabled_stdio_servers",
                        lambda: {"files": {"transport": "stdio",
                                           "command": "npx"}})
    servers = M._all_servers()
    assert servers["web"] == {"transport": "http", "url": "http://a:1"}
    assert servers["files"]["command"] == "npx"


def test_http_wins_a_name_clash(monkeypatch):
    from aiforge_core.config import mcp_registry
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "x=http://a:1")
    monkeypatch.setattr(mcp_registry, "enabled_stdio_servers",
                        lambda: {"x": {"transport": "stdio", "command": "npx"}})
    assert M._all_servers()["x"]["transport"] == "http"


def test_both_maps_are_reported(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "a=http://a:1")
    out = M.list_endpoints()
    assert out == {"ok": True, "endpoints": {"a": "http://a:1"}, "stdio": {}}


# ─── the HTTP POST ─────────────────────────────────────────────────────


@pytest.fixture
def http(monkeypatch):
    import urllib.request
    state: dict = {"body": b'{"result": {"tools": []}}', "raise": None,
                   "seen": {}}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            return state["body"][:n] if n else state["body"]

    def _urlopen(req, timeout=None, context=None):
        state["seen"] = {"url": req.full_url, "timeout": timeout,
                         "payload": json.loads(req.data.decode()),
                         "headers": dict(req.headers)}
        if state["raise"]:
            raise state["raise"]
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return state


def test_a_json_rpc_call_is_posted_and_parsed(http):
    out = M._post_json("http://127.0.0.1:9000/mcp", {"method": "tools/list"})
    assert out == {"result": {"tools": []}}
    assert http["seen"]["timeout"] == M._HTTP_TIMEOUT_S
    assert http["seen"]["headers"]["Content-type"] == "application/json"


def test_plain_http_to_a_remote_host_is_refused(http):
    """It would put API keys and prompt text on the wire in cleartext."""
    with pytest.raises(OSError, match="refusing MCP endpoint"):
        M._post_json("http://mcp.example.com/mcp", {})


def test_a_stdio_command_is_never_handed_to_urllib(http):
    """urllib would raise ValueError('unknown url type'), which no caller
    catches — it has to come back as an OSError instead."""
    with pytest.raises(OSError, match="refusing MCP endpoint"):
        M._post_json("npx server-filesystem/mcp", {})


def test_a_non_json_response_is_kept_raw(http):
    http["body"] = b"<html>gateway error</html>"
    assert M._post_json("http://127.0.0.1:9000/mcp", {})["raw"].startswith("<html>")


def test_a_huge_response_is_capped_and_flagged(http):
    http["body"] = b'{"x": "' + b"y" * (M._RESPONSE_CAP_BYTES + 50) + b'"}'
    out = M._post_json("http://127.0.0.1:9000/mcp", {})
    assert out["_truncated"] is True


# ─── bridging the async SDK ────────────────────────────────────────────


async def _answer():
    return "done"


def test_a_coroutine_runs_on_the_callers_thread():
    assert M._run_async(_answer()) == "done"


def test_it_still_works_from_inside_a_running_loop():
    """asyncio.run refuses there, so a private loop on a throwaway thread
    takes over."""
    async def _outer():
        return M._run_async(_answer())
    assert asyncio.run(_outer()) == "done"


def test_an_error_from_the_threaded_path_reaches_the_caller():
    async def _boom():
        raise RuntimeError("server died")

    async def _outer():
        return M._run_async(_boom())
    outer = _outer()
    with pytest.raises(RuntimeError, match="server died"):
        asyncio.run(outer)


# ─── shaping what a server returned ────────────────────────────────────


def test_a_tool_is_summarised_from_an_object_or_a_dict():
    obj = pytypes.SimpleNamespace(name="read_file", description="reads a file")
    assert M._tool_summary(obj) == {"name": "read_file",
                                    "description": "reads a file"}
    assert M._tool_summary({"name": "a", "description": None}) == {
        "name": "a", "description": ""}


def test_a_long_description_is_trimmed():
    assert len(M._tool_summary({"name": "a",
                                "description": "x" * 500})["description"]) == 120


def test_a_pydantic_result_is_dumped_as_json():
    res = pytypes.SimpleNamespace(model_dump=lambda mode=None: {"content": [1]})
    assert M._serialize_result(res) == {"content": [1]}


def test_an_older_dump_signature_still_works():
    def _dump(mode=None):
        if mode is not None:
            raise TypeError("no mode arg")
        return {"content": []}
    assert M._serialize_result(pytypes.SimpleNamespace(model_dump=_dump)) == {
        "content": []}


def test_an_undumpable_result_falls_back_to_its_text():
    def _dump(mode=None):
        raise RuntimeError("cycle")
    res = pytypes.SimpleNamespace(model_dump=_dump)
    assert "raw" in M._serialize_result(res)


@pytest.mark.parametrize("res,expected", [
    (None, {}), ({"a": 1}, {"a": 1}), ("plain", {"raw": "plain"})])
def test_everything_else_is_passed_through_or_stringified(res, expected):
    assert M._serialize_result(res) == expected


# ─── listing tools ─────────────────────────────────────────────────────


@pytest.fixture
def one_http_server(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS", "web=http://127.0.0.1:9000")
    return "web"


def test_the_tools_of_an_http_server_are_listed(one_http_server, http):
    http["body"] = json.dumps({"result": {"tools": [
        {"name": "fetch", "description": "get a page"}, "junk"]}}).encode()
    out = M.list_tools("web")
    assert out["ok"] is True
    assert out["tools"] == [{"name": "fetch",
                                                   "description": "get a page"}]
    assert http["seen"]["url"] == "http://127.0.0.1:9000/mcp"
    assert http["seen"]["payload"]["method"] == "tools/list"


def test_an_http_error_reports_its_status(one_http_server, http):
    http["raise"] = urllib.error.HTTPError("http://127.0.0.1:9000/mcp", 503, "busy", {}, None)
    assert M.list_tools("web") == {"ok": False, "error": "http_error",
                                   "status": 503, "endpoint": "web"}


def test_an_unreachable_server_is_a_soft_failure(one_http_server, http):
    http["raise"] = OSError("connection refused")
    out = M.list_tools("web")
    assert out["error"] == "connection_failed"
    assert "refused" in out["detail"]


def test_an_unknown_endpoint_names_the_ones_that_exist(one_http_server):
    out = M.list_tools("ghost")
    assert out["error"] == "unknown_endpoint"
    assert out["allowed"] == ["web"]


def test_with_nothing_configured_there_is_nothing_to_list():
    assert M.list_tools("web") == {"ok": False, "error": M._NO_SERVERS_ERROR}


# ─── calling a tool ────────────────────────────────────────────────────


def test_a_tool_call_is_dispatched_and_its_result_returned(one_http_server,
                                                            http):
    http["body"] = json.dumps({"result": {"content": "hello"}}).encode()
    out = M.call_tool("web", "fetch", {"url": "http://x"})
    assert out["result"] == {"content": "hello"}
    assert out["tool"] == "fetch"
    params = http["seen"]["payload"]["params"]
    assert params == {"name": "fetch", "arguments": {"url": "http://x"}}


def test_a_tool_call_with_no_arguments_sends_an_empty_map(one_http_server,
                                                           http):
    M.call_tool("web", "ping")
    assert http["seen"]["payload"]["params"]["arguments"] == {}


def test_an_error_the_server_reports_is_surfaced(one_http_server, http):
    http["body"] = json.dumps({"error": {"code": -32601,
                                         "message": "no such tool"}}).encode()
    out = M.call_tool("web", "nope")
    assert out["error"] == "mcp_error"
    assert out["detail"]["code"] == -32601


def test_a_call_needs_a_tool_name(one_http_server):
    assert M.call_tool("web", "  ")["error"] == "missing_tool"


def test_a_call_to_an_unknown_endpoint_is_refused(one_http_server):
    assert M.call_tool("ghost", "t")["error"] == "unknown_endpoint"


def test_a_failed_call_reports_the_transport_error(one_http_server, http):
    http["raise"] = urllib.error.HTTPError("http://127.0.0.1:9000/mcp", 500, "x", {}, None)
    assert M.call_tool("web", "t")["status"] == 500
    http["raise"] = OSError("refused")
    assert M.call_tool("web", "t")["error"] == "connection_failed"


# ─── the local stdio transport ─────────────────────────────────────────


@pytest.fixture
def stdio(monkeypatch):
    from aiforge_core.config import mcp_registry
    state: dict = {"result": pytypes.SimpleNamespace(
        tools=[{"name": "read_file", "description": "read"}]),
        "raise": None, "available": True}
    monkeypatch.setattr(mcp_registry, "enabled_stdio_servers",
                        lambda: {"files": {"transport": "stdio",
                                           "command": "npx",
                                           "args": ["server-filesystem"]}})
    monkeypatch.setattr(M, "_stdio_available", lambda: state["available"])

    def _run(coro):
        coro.close()                    # never actually spawn anything
        if state["raise"]:
            raise state["raise"]
        return state["result"]
    monkeypatch.setattr(M, "_run_async", _run)
    return state


def test_a_stdio_servers_tools_are_listed(stdio):
    out = M.list_tools("files")
    assert out["ok"] is True
    assert out["transport"] == "stdio"
    assert out["tools"] == [{"name": "read_file", "description": "read"}]


def test_a_stdio_result_shaped_as_a_dict_also_works(stdio):
    stdio["result"] = {"tools": [{"name": "a", "description": ""}]}
    assert M.list_tools("files")["tools"] == [{"name": "a", "description": ""}]


def test_a_server_that_returned_no_tools_is_still_ok(stdio):
    stdio["result"] = pytypes.SimpleNamespace(tools=None)
    assert M.list_tools("files") == {"ok": True, "endpoint": "files",
                                     "transport": "stdio", "tools": []}


def test_a_missing_command_says_exactly_that(stdio):
    stdio["raise"] = FileNotFoundError("npx not found")
    out = M.list_tools("files")
    assert out["error"] == "command_not_found"
    assert "npx" in out["detail"]


def test_a_server_that_will_not_start_is_a_soft_failure(stdio):
    stdio["raise"] = RuntimeError("handshake failed")
    assert M.list_tools("files")["error"] == "connection_failed"


def test_without_the_sdk_stdio_servers_say_so(stdio):
    stdio["available"] = False
    assert M.list_tools("files")["error"] == "mcp_sdk_unavailable"
    assert M.call_tool("files", "read_file")["error"] == "mcp_sdk_unavailable"


def test_a_stdio_tool_call_returns_a_serialised_result(stdio):
    stdio["result"] = pytypes.SimpleNamespace(
        model_dump=lambda mode=None: {"content": [{"text": "hi"}]})
    out = M.call_tool("files", "read_file", {"path": "/x"})
    assert out["ok"] is True
    assert out["transport"] == "stdio"
    assert out["result"] == {"content": [{"text": "hi"}]}


def test_a_stdio_call_reports_a_missing_command(stdio):
    stdio["raise"] = FileNotFoundError("npx")
    assert M.call_tool("files", "t")["error"] == "command_not_found"


def test_a_stdio_call_that_dies_is_soft(stdio):
    stdio["raise"] = RuntimeError("broken pipe")
    assert M.call_tool("files", "t")["error"] == "connection_failed"


# ─── the dispatcher ────────────────────────────────────────────────────


def test_listing_endpoints_needs_no_servers():
    assert M.mcp("list_endpoints")["ok"] is True


@pytest.mark.parametrize("command", ["list_tools", "call_tool"])
def test_with_nothing_configured_no_network_is_touched(command, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("hit the network"))
    assert M.mcp(command, endpoint="web", tool="t")["error"] == \
        M._NO_SERVERS_ERROR


def test_each_command_needs_its_arguments(one_http_server):
    assert M.mcp("list_tools")["error"] == "missing_endpoint"
    assert M.mcp("call_tool")["error"] == "missing_endpoint"
    assert M.mcp("call_tool", endpoint="web")["error"] == "missing_tool"


def test_a_command_nobody_knows_is_refused(one_http_server):
    assert M.mcp("do_magic") == {"ok": False, "error": "unknown_command",
                                 "command": "do_magic"}


def test_the_dispatcher_reaches_both_operations(one_http_server, http):
    assert M.mcp("list_tools", endpoint="web")["ok"] is True
    http["body"] = json.dumps({"result": {}}).encode()
    assert M.mcp("call_tool", endpoint="web", tool="fetch")["ok"] is True
