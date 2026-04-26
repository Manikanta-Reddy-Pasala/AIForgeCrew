"""Sync wrapper around the MCP streamable-http client.

Used by the chat + doer GA loops to call OneShell ops MCP servers
(mongo / k8s / tekton / tally) without depending on smolagents'
ToolCollection. KISS: one process per call — open client, list_tools
(cached), call_tool, close. Error mapping returns readable strings;
never raises into the agent loop.

The mcp Python SDK only exposes async APIs, so we wrap each call with
``asyncio.new_event_loop().run_until_complete``. Per-call latency is
network-bound (sub-100ms LAN), so the event-loop spin-up cost is
negligible relative to actual MCP RTT.

Caching:
- Tool name → server URL map cached at module load (env-driven).
- Per-server tool catalogue cached for 5 minutes.

Allowlist + reject behaviour live in the caller — this module is a
thin transport.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Iterable


# Default server map mirrors the NUC oneshell-mcp deployment
# (QA tier on :881x, prod on :882x). Override per server with
# AIFORGE_MCP_<NAME>=http://host:port/mcp env vars.
DEFAULT_OPS_SERVERS: dict[str, str] = {
    "mongo":  "http://127.0.0.1:8810/mcp",
    "k8s":    "http://127.0.0.1:8811/mcp",
    "tekton": "http://127.0.0.1:8812/mcp",
    "tally":  "http://127.0.0.1:8813/mcp",
}


def resolved_servers() -> dict[str, str]:
    """Return the active server map after env overrides."""
    out = dict(DEFAULT_OPS_SERVERS)
    for short in list(out):
        env = os.environ.get(f"AIFORGE_MCP_{short.upper()}")
        if env:
            out[short] = env
    return out


_TOOL_CATALOGUE_TTL_S = 300
_tool_catalogue_cache: dict[str, tuple[float, list[dict]]] = {}


def list_tools(server_url: str) -> list[dict]:
    """Discover the server's tool list. Cached for 5 minutes."""
    now = time.time()
    cached = _tool_catalogue_cache.get(server_url)
    if cached is not None and (now - cached[0]) < _TOOL_CATALOGUE_TTL_S:
        return cached[1]

    async def _do() -> list[dict]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                resp = await sess.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": (t.description or "")[:300],
                        "input_schema": (t.inputSchema or {}),
                    }
                    for t in (resp.tools or [])
                ]

    try:
        tools = _run(_do())
    except Exception as exc:
        # Soft-fail: empty catalogue. Caller advertises no ops tools
        # for this server but the rest of chat/doer keeps working.
        print(f"[mcp_http] list_tools({server_url}) failed: {exc}")
        tools = []
    _tool_catalogue_cache[server_url] = (now, tools)
    return tools


def call_tool(server_url: str, tool: str, args: dict) -> str:
    """Invoke ``tool`` on ``server_url``. Returns stringified result.

    Errors return as ``f"error: {msg}"`` so the agent loop can keep
    going.
    """
    async def _do() -> str:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                resp = await sess.call_tool(tool, args or {})
                if resp.isError:
                    return f"error: {_text_of(resp.content)[:1500]}"
                return _text_of(resp.content)[:6000]

    try:
        return _run(_do())
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def all_tools_with_origin(
    servers: Iterable[tuple[str, str]] | None = None,
) -> list[dict]:
    """Cross-server discovery. Returns one entry per (server, tool).

    Each entry: ``{server, tool, description, input_schema}``. Caller
    builds the OpenAI-function-style schema from this list.
    """
    src = dict(servers) if servers else resolved_servers()
    out: list[dict] = []
    for short, url in src.items():
        for t in list_tools(url):
            out.append({
                "server": short,
                "url": url,
                "tool": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            })
    return out


def render_schema_for_openai(
    discovered: list[dict],
    *,
    prefix: str = "ops_",
) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """Build OpenAI-function tool schema list + name→(server, raw_tool) map.

    The tool name we expose to the LLM is ``f"{prefix}{server}_{tool}"``
    so a list_pods on k8s becomes ``ops_k8s_list_pods``. Caller stores
    the returned map and uses it to route ``do_<full_name>`` back to
    the correct server URL.
    """
    schemas: list[dict] = []
    name_map: dict[str, tuple[str, str]] = {}
    for entry in discovered:
        full_name = f"{prefix}{entry['server']}_{entry['tool']}"
        name_map[full_name] = (entry["url"], entry["tool"])
        # Strip non-OpenAI properties from input_schema (anyOf, $ref
        # often confuse stricter validators). Pass through as-is for
        # most cases — the model usually fills basic types fine.
        params = entry.get("input_schema") or {
            "type": "object", "properties": {}, "required": [],
        }
        schemas.append({
            "type": "function",
            "function": {
                "name": full_name,
                "description": entry["description"][:300],
                "parameters": params,
            },
        })
    return schemas, name_map


# ───────── helpers ─────────────────────────────────────────────────


def _run(coro):
    """Spin up a fresh event loop per call. Avoids bleed across the
    sync boundary used inside FastAPI sync handlers / GA generators."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _text_of(content) -> str:
    """Flatten MCP content blocks into a string for agent consumption."""
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text") and getattr(block, "text", None):
            parts.append(str(block.text))
        elif isinstance(block, dict):
            t = block.get("text") or block.get("content") or ""
            if t:
                parts.append(str(t))
        else:
            parts.append(str(block))
    out = "\n".join(parts)
    if not out:
        try:
            return json.dumps([str(b) for b in content])[:6000]
        except Exception:
            return str(content)[:6000]
    return out
