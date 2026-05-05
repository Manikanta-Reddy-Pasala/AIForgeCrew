"""Lightweight MCP client for the oneshell-mcp servers on NUC.

Servers expose Mongo / NATS / Redis / saga state via JSON-RPC over
HTTP+SSE at:
  QA   :8810 mongo, :8811 nats, :8812 redis, :8813 saga
  prod :8820 mongo, :8821 nats, :8822 redis, :8823 saga

The Doer / IntegrationTestAgent calls into this to discover real test
data (a valid `businessId` with payments, a real saga id, etc.) so
integration smokes hit live data, not synthesised fixtures.

Pure stdlib (urllib + SSE parsing) — no external SDK. Sessions are
short-lived per call_tool: initialize → tools/call → close.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any


_DEFAULT_HOST = os.environ.get("AIFORGE_MCP_HOST", "192.168.70.191")
_TIER_PORTS = {
    "qa": {"mongo": 8810, "nats": 8811, "redis": 8812, "saga": 8813},
    "prod": {"mongo": 8820, "nats": 8821, "redis": 8822, "saga": 8823},
}


class MCPError(RuntimeError):
    pass


def _post(url: str, payload: dict, sid: str | None = None,
          timeout: int = 15) -> tuple[dict | None, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            new_sid = resp.headers.get("Mcp-Session-Id")
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise MCPError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    parsed: dict | None = None
    for line in text.splitlines():
        if line.startswith("data: "):
            parsed = json.loads(line[6:])
    return parsed, new_sid


def _resolve_url(tier: str, server: str) -> str:
    tier_map = _TIER_PORTS.get(tier)
    if tier_map is None:
        raise MCPError(f"unknown tier {tier!r}")
    port = tier_map.get(server)
    if port is None:
        raise MCPError(f"unknown server {server!r} in tier {tier!r}")
    return f"http://{_DEFAULT_HOST}:{port}/mcp"


def call_tool(server: str, tool: str, arguments: dict | None = None,
              tier: str = "qa", timeout: int = 30) -> dict:
    """Initialize, call one tool, return the parsed result.

    Returns the ``result.content`` payload (list of {type, text/...}
    blocks per MCP spec), or raises ``MCPError`` on protocol failure.
    Production-tier access requires ``AIFORGE_ALLOW_PROD_MCP=1`` env.
    """
    if tier == "prod" and os.environ.get("AIFORGE_ALLOW_PROD_MCP") != "1":
        raise MCPError("prod MCP requires AIFORGE_ALLOW_PROD_MCP=1")
    url = _resolve_url(tier, server)
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "aiforge", "version": "1"},
        },
    }
    init_resp, sid = _post(url, init_payload, timeout=timeout)
    if not init_resp or "result" not in init_resp:
        raise MCPError(f"initialize failed: {init_resp}")
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
          sid=sid, timeout=timeout)
    call_payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    resp, _ = _post(url, call_payload, sid=sid, timeout=timeout)
    if not resp:
        raise MCPError("tools/call returned no data")
    if "error" in resp:
        raise MCPError(f"tool {tool} error: {resp['error']}")
    return resp.get("result", {})


def find_test_business(collection_with_data: str = "paymentIn",
                       tier: str = "qa") -> str | None:
    """Pick a businessId that actually has rows in the given collection.

    Used by IntegrationTestAgent to seed an integration smoke with a
    real business value so the new endpoint isn't curl'd against an
    empty cursor.

    Returns the businessId string or None on failure.
    """
    try:
        out = call_tool(
            "mongo",
            "aggregate",
            {
                "collection": collection_with_data,
                "pipeline": [
                    {"$match": {"businessId": {"$exists": True, "$ne": None}}},
                    {"$group": {"_id": "$businessId", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}},
                    {"$limit": 1},
                ],
            },
            tier=tier,
            timeout=20,
        )
    except MCPError:
        return None
    content = out.get("content") or []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0].get("_id") or parsed[0].get("businessId")
        if isinstance(parsed, dict):
            rows = (parsed.get("docs") or parsed.get("results")
                    or parsed.get("data") or [parsed])
            if rows and isinstance(rows[0], dict):
                return rows[0].get("_id") or rows[0].get("businessId")
    return None


__all__ = ["MCPError", "call_tool", "find_test_business"]
