"""MCP (Model Context Protocol) client tool — closes EVAL-4 integration
follow-up from 2026-04-23.

The Doer can list tools on configured MCP endpoints (default = oneshell-mcp
QA tier on NUC: mongo/k8s/tekton/tally servers) and invoke them by name.
Endpoints can be HTTP (FastMCP / SSE) or stdio (npx server-github style).

Two transports:
  * **HTTP/SSE** — pure JSON-over-HTTP (FastMCP semantics, the shape
    oneshell-mcp ships). Talks to a hosted MCP server.
  * **stdio** — spawns a LOCAL child process (e.g. ``npx
    @modelcontextprotocol/server-filesystem``) via the ``mcp`` python SDK.
    LOCAL-only: a subprocess on this machine, no network, no phone-home.

The stdio transport comes from the marketplace registry
(``enabled_stdio_servers()``) as a parallel ``{name: {command, args, env}}``
map; the HTTP endpoint map (``{name: url}``) is untouched.

Soft-error contract. Connection / parse / spawn errors return ``{ok: False,
error}`` so the agent loop survives a dead MCP endpoint or a missing ``npx``.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from aiforge_core.net.ssl import context_for as _ssl_context_for

from ._trace import emit

# MCP python SDK (stdio transport). Imported at module scope so it can be
# monkeypatched in tests and probed once. Soft: absence degrades stdio calls
# to a clean ``{ok: False, error}`` instead of crashing the agent loop.
try:  # pragma: no cover - exercised via the _MCP_SDK_OK flag in tests
    from mcp import ClientSession as _ClientSession
    from mcp.client.stdio import (
        StdioServerParameters as _StdioServerParameters,
        stdio_client as _stdio_client,
    )
    _MCP_SDK_OK = True
except Exception:  # noqa: BLE001
    _ClientSession = None       # type: ignore[assignment]
    _StdioServerParameters = None  # type: ignore[assignment]
    _stdio_client = None        # type: ignore[assignment]
    _MCP_SDK_OK = False

# No servers ship by default (operator reset 2026-06-26). Endpoints come
# ONLY from the env CSV AIFORGE_MCP_ENDPOINTS="name=url,name=url,..."; with
# none configured the `mcp` tool fail-softs without any network probing.
_DEFAULT_ENDPOINTS: tuple[tuple[str, str], ...] = ()

_NO_SERVERS_ERROR = "no MCP servers configured"

_HTTP_TIMEOUT_S = 15
_RESPONSE_CAP_BYTES = 16 * 1024


def _load_endpoints() -> dict[str, str]:
    # Base = bundled defaults (currently none). The env CSV and the marketplace
    # registry layer on top (registry wins on a name clash — it's the explicit
    # user choice in the UI).
    out: dict[str, str] = dict(_DEFAULT_ENDPOINTS)
    raw = os.environ.get("AIFORGE_MCP_ENDPOINTS", "").strip()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, url = entry.split("=", 1)
        out[name.strip()] = url.strip()
    # Marketplace-installed + enabled HTTP servers (one-click installer). Soft:
    # a missing/broken registry never breaks the env-configured endpoints.
    try:
        from aiforge_core.config import mcp_registry
        out.update(mcp_registry.enabled_endpoints())
    except Exception:  # noqa: BLE001
        pass
    return out


def _load_stdio_servers() -> dict[str, dict]:
    """LOCAL stdio servers ``{name: {transport, command, args, env}}`` from the
    marketplace registry. Soft: a missing/broken registry → no stdio servers."""
    out: dict[str, dict] = {}
    try:
        from aiforge_core.config import mcp_registry
        out.update(mcp_registry.enabled_stdio_servers())
    except Exception:  # noqa: BLE001
        pass
    return out


def _all_servers() -> dict[str, dict]:
    """Unified ``{name: cfg}`` map. HTTP rows become ``{transport, url}``; stdio
    rows carry ``{transport, command, args, env}``. HTTP wins on a name clash."""
    servers: dict[str, dict] = {}
    for name, url in _load_endpoints().items():
        servers[name] = {"transport": "http", "url": url}
    for name, cfg in _load_stdio_servers().items():
        servers.setdefault(name, cfg)
    return servers


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    # Only HTTP(S) is supported here; a stdio endpoint (e.g. "npx server-...")
    # would make urllib.request.Request raise ValueError("unknown url type"),
    # which the callers don't catch → it escapes into the agent loop. Reject
    # cleanly via OSError so the existing soft-error handlers cover it.
    from aiforge_core.net.url_policy import check
    why = check(url)
    if why:
        raise OSError(f"refusing MCP endpoint {url!r}: {why}")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AIForgeCrew-MCP-Client/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(
        req, timeout=_HTTP_TIMEOUT_S, context=_ssl_context_for(url)
    ) as resp:
        raw = resp.read(_RESPONSE_CAP_BYTES + 1)
    truncated = len(raw) > _RESPONSE_CAP_BYTES
    text = raw[:_RESPONSE_CAP_BYTES].decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"raw": text}
    if isinstance(parsed, dict) and truncated:
        parsed["_truncated"] = True
    return parsed


# ----------------------------- stdio transport ----------------------------- #
# The MCP SDK is async; the ``mcp`` tool is sync. We open a fresh connection
# per call (simple, no lifecycle state — cache later if it matters) and bridge
# the async SDK to the sync caller via ``asyncio.run`` (with a threaded fallback
# for the rare case we're already inside a running event loop).


def _run_async(coro) -> Any:
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # Already inside a running loop → run the (never-started) coroutine on a
        # private loop in a throwaway thread so we don't reuse it.
        import threading
        box: dict[str, Any] = {}

        def _worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                box["v"] = loop.run_until_complete(coro)
            except BaseException as exc:  # noqa: BLE001
                box["e"] = exc
            finally:
                loop.close()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join()
        if "e" in box:
            raise box["e"]
        return box.get("v")


async def _stdio_session(cfg: dict, op) -> Any:
    """Spawn the LOCAL stdio server, initialize a session, run ``op(session)``.

    ``env`` inherits the parent process env (so ``npx``/``node`` resolve on
    PATH) with the server's declared overrides layered on top."""
    env = {**os.environ, **(cfg.get("env") or {})}
    params = _StdioServerParameters(
        command=cfg["command"],
        args=list(cfg.get("args") or []),
        env=env,
    )
    async with _stdio_client(params) as (read, write):
        async with _ClientSession(read, write) as session:
            await session.initialize()
            return await op(session)


def _stdio_available() -> bool:
    return bool(_MCP_SDK_OK and _stdio_client is not None
                and _ClientSession is not None
                and _StdioServerParameters is not None)


def _tool_summary(t: Any) -> dict[str, str]:
    if isinstance(t, dict):
        name, desc = t.get("name", ""), t.get("description", "") or ""
    else:
        name = getattr(t, "name", "") or ""
        desc = getattr(t, "description", "") or ""
    return {"name": name, "description": (desc or "")[:120]}


def _serialize_result(res: Any) -> Any:
    if res is None:
        return {}
    dump = getattr(res, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
        except Exception:  # noqa: BLE001
            return {"raw": str(res)[:_RESPONSE_CAP_BYTES]}
    if isinstance(res, dict):
        return res
    return {"raw": str(res)[:_RESPONSE_CAP_BYTES]}


def _stdio_list_tools(endpoint: str, cfg: dict) -> dict[str, Any]:
    if not _stdio_available():
        return {"ok": False, "error": "mcp_sdk_unavailable", "endpoint": endpoint,
                "detail": "the 'mcp' python SDK is not installed"}

    async def _op(session):
        return await session.list_tools()

    try:
        res = _run_async(_stdio_session(cfg, _op))
    except FileNotFoundError as exc:
        return {"ok": False, "error": "command_not_found",
                "endpoint": endpoint, "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — soft-fail: never crash the loop
        return {"ok": False, "error": "connection_failed",
                "endpoint": endpoint, "detail": str(exc)[:200]}
    tools_raw = getattr(res, "tools", None)
    if tools_raw is None and isinstance(res, dict):
        tools_raw = res.get("tools")
    summary = [_tool_summary(t) for t in (tools_raw or [])]
    emit("MCPCall", {"action": "list_tools", "endpoint": endpoint,
                     "transport": "stdio", "count": len(summary)})
    return {"ok": True, "endpoint": endpoint, "transport": "stdio",
            "tools": summary}


def _stdio_call_tool(endpoint: str, cfg: dict, tool: str,
                     arguments: dict[str, Any]) -> dict[str, Any]:
    if not _stdio_available():
        return {"ok": False, "error": "mcp_sdk_unavailable", "endpoint": endpoint,
                "tool": tool, "detail": "the 'mcp' python SDK is not installed"}

    async def _op(session):
        return await session.call_tool(tool, arguments)

    try:
        res = _run_async(_stdio_session(cfg, _op))
    except FileNotFoundError as exc:
        return {"ok": False, "error": "command_not_found",
                "endpoint": endpoint, "tool": tool, "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — soft-fail
        return {"ok": False, "error": "connection_failed",
                "endpoint": endpoint, "tool": tool, "detail": str(exc)[:200]}
    emit("MCPCall", {"action": "call_tool", "endpoint": endpoint,
                     "transport": "stdio", "tool": tool})
    return {"ok": True, "endpoint": endpoint, "transport": "stdio",
            "tool": tool, "result": _serialize_result(res)}


def list_endpoints() -> dict[str, Any]:
    """Return the active endpoint name → url mapping (HTTP) plus the LOCAL
    stdio server map."""
    return {"ok": True, "endpoints": _load_endpoints(),
            "stdio": _load_stdio_servers()}


def list_tools(endpoint: str) -> dict[str, Any]:
    """List MCP tools exposed by ``endpoint`` — HTTP (FastMCP JSON-RPC
    tools/list) or LOCAL stdio (MCP SDK), routed by the server's transport."""
    servers = _all_servers()
    if not servers:
        return {"ok": False, "error": _NO_SERVERS_ERROR}
    cfg = servers.get(endpoint)
    if cfg is None:
        return {"ok": False, "error": "unknown_endpoint",
                "endpoint": endpoint, "allowed": sorted(servers.keys())}
    if (cfg.get("transport") or "http") == "stdio":
        return _stdio_list_tools(endpoint, cfg)
    url = cfg.get("url") or ""
    try:
        resp = _post_json(f"{url}/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_error",
                "status": exc.code, "endpoint": endpoint}
    except OSError as exc:
        return {"ok": False, "error": "connection_failed",
                "endpoint": endpoint, "detail": str(exc)[:200]}
    tools_raw = (resp.get("result") or {}).get("tools") or []
    summary = [
        {"name": t.get("name", ""),
         "description": (t.get("description", "") or "")[:120]}
        for t in tools_raw if isinstance(t, dict)
    ]
    emit("MCPCall", {"action": "list_tools", "endpoint": endpoint,
                     "count": len(summary)})
    return {"ok": True, "endpoint": endpoint, "tools": summary}


def call_tool(
    endpoint: str, tool: str, arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke ``tool`` on ``endpoint`` with ``arguments`` — HTTP (JSON-RPC
    tools/call) or LOCAL stdio (MCP SDK), routed by the server's transport."""
    servers = _all_servers()
    if not servers:
        return {"ok": False, "error": _NO_SERVERS_ERROR}
    cfg = servers.get(endpoint)
    if cfg is None:
        return {"ok": False, "error": "unknown_endpoint",
                "endpoint": endpoint, "allowed": sorted(servers.keys())}
    if not tool or not tool.strip():
        return {"ok": False, "error": "missing_tool"}
    args = arguments or {}
    if (cfg.get("transport") or "http") == "stdio":
        return _stdio_call_tool(endpoint, cfg, tool, args)
    url = cfg.get("url") or ""
    try:
        resp = _post_json(f"{url}/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_error",
                "status": exc.code, "endpoint": endpoint, "tool": tool}
    except OSError as exc:
        return {"ok": False, "error": "connection_failed",
                "endpoint": endpoint, "tool": tool,
                "detail": str(exc)[:200]}
    err = resp.get("error")
    if err:
        return {"ok": False, "error": "mcp_error",
                "endpoint": endpoint, "tool": tool, "detail": err}
    result = resp.get("result") or {}
    emit("MCPCall", {"action": "call_tool", "endpoint": endpoint,
                     "tool": tool})
    return {"ok": True, "endpoint": endpoint, "tool": tool, "result": result}


def mcp(
    command: str,
    *,
    endpoint: str | None = None,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatcher: ``list_endpoints`` | ``list_tools`` | ``call_tool``."""
    if command == "list_endpoints":
        return list_endpoints()
    # No servers configured → fail-soft with a clean message and zero network.
    if command in ("list_tools", "call_tool") and not _all_servers():
        return {"ok": False, "error": _NO_SERVERS_ERROR}
    if command == "list_tools":
        if not endpoint:
            return {"ok": False, "error": "missing_endpoint"}
        return list_tools(endpoint)
    if command == "call_tool":
        if not endpoint:
            return {"ok": False, "error": "missing_endpoint"}
        if not tool:
            return {"ok": False, "error": "missing_tool"}
        return call_tool(endpoint, tool, arguments)
    return {"ok": False, "error": "unknown_command", "command": command}


__all__ = ["mcp", "list_endpoints", "list_tools", "call_tool"]
