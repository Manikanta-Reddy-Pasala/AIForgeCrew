"""MCP (Model Context Protocol) client tool — closes EVAL-4 integration
follow-up from 2026-04-23.

The Doer can list tools on configured MCP endpoints (default = oneshell-mcp
QA tier on NUC: mongo/k8s/tekton/tally servers) and invoke them by name.
Endpoints can be HTTP (FastMCP / SSE) or stdio (npx server-github style).

KISS: pure HTTP transport for now via FastMCP's JSON-over-HTTP semantics —
the same shape oneshell-mcp ships. Stdio path mocked-ready but unwired
(adds via mcpadapt in a follow-up only if needed).

Soft-error contract. Connection / parse errors return ``{ok: False,
error}`` so the agent loop survives a dead MCP endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ._trace import emit

# Default endpoints — matches oneshell-mcp QA tier on NUC (memory:
# project_oneshell_mcp_2026-04-24). Override via env CSV
# AIFORGE_MCP_ENDPOINTS="name=url,name=url,...".
_DEFAULT_ENDPOINTS = (
    ("oneshell-mongo", "http://192.168.70.115:8810"),
    ("oneshell-k8s",   "http://192.168.70.115:8811"),
    ("oneshell-tekton", "http://192.168.70.115:8812"),
    ("oneshell-tally", "http://192.168.70.115:8813"),
)

_HTTP_TIMEOUT_S = 15
_RESPONSE_CAP_BYTES = 16 * 1024


def _load_endpoints() -> dict[str, str]:
    raw = os.environ.get("AIFORGE_MCP_ENDPOINTS", "").strip()
    if not raw:
        return dict(_DEFAULT_ENDPOINTS)
    out: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, url = entry.split("=", 1)
        out[name.strip()] = url.strip()
    return out


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
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
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
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


def list_endpoints() -> dict[str, Any]:
    """Return the active endpoint name → url mapping."""
    return {"ok": True, "endpoints": _load_endpoints()}


def list_tools(endpoint: str) -> dict[str, Any]:
    """List MCP tools exposed by ``endpoint`` (FastMCP JSON-RPC tools/list)."""
    endpoints = _load_endpoints()
    url = endpoints.get(endpoint)
    if url is None:
        return {"ok": False, "error": "unknown_endpoint",
                "endpoint": endpoint, "allowed": sorted(endpoints.keys())}
    try:
        resp = _post_json(f"{url}/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_error",
                "status": exc.code, "endpoint": endpoint}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
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
    """Invoke ``tool`` on ``endpoint`` with ``arguments`` (JSON-RPC tools/call)."""
    endpoints = _load_endpoints()
    url = endpoints.get(endpoint)
    if url is None:
        return {"ok": False, "error": "unknown_endpoint",
                "endpoint": endpoint, "allowed": sorted(endpoints.keys())}
    if not tool or not tool.strip():
        return {"ok": False, "error": "missing_tool"}
    args = arguments or {}
    try:
        resp = _post_json(f"{url}/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "http_error",
                "status": exc.code, "endpoint": endpoint, "tool": tool}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
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
