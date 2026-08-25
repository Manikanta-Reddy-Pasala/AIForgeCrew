"""MCP marketplace/installer + graph_rag MCP tool-call routes.
Extracted from api.py (behavior-preserving) — was split across two locations.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


# ─────────────────────── MCP marketplace / installer ───────────────────────
class _McpInstallBody(BaseModel):
    catalog_id: str = Field(..., min_length=1)
    url: str | None = Field(None, description="override catalog url (required for custom)")
    name: str | None = Field(None, description="override display name")
    api_key: str | None = Field(None, description="optional bearer/api key")


class _McpUpdateBody(BaseModel):
    name: str | None = None
    url: str | None = None
    description: str | None = None
    enabled: bool | None = None
    api_key: str | None = None


def _default_dsn() -> str:
    from aiforge_core.config.env import default_pg_dsn
    return default_pg_dsn()


@router.get("/api/mcp/catalog")
def mcp_catalog() -> dict:
    """The curated MCP marketplace catalog (browse → one-click install)."""
    from aiforge_core.config import mcp_registry
    return {"catalog": mcp_registry.load_catalog()}


@router.get("/api/mcp/servers")
def mcp_servers() -> dict:
    """Installed MCP servers (secrets stripped)."""
    from aiforge_core.config import mcp_registry
    return {"servers": mcp_registry.list_servers()}


@router.post("/api/mcp/servers", status_code=201)
def mcp_server_install(body: _McpInstallBody) -> dict:
    from aiforge_core.config import mcp_registry
    try:
        return mcp_registry.install_from_catalog(
            body.catalog_id, url=body.url, api_key=body.api_key, name=body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/api/mcp/servers/{server_id}")
def mcp_server_update(server_id: str, body: _McpUpdateBody) -> dict:
    from aiforge_core.config import mcp_registry
    row = mcp_registry.update_server(
        server_id, name=body.name, url=body.url, description=body.description,
        enabled=body.enabled, api_key=body.api_key)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    return row


@router.delete("/api/mcp/servers/{server_id}", status_code=204)
def mcp_server_delete(server_id: str) -> None:
    from aiforge_core.config import mcp_registry
    if not mcp_registry.remove_server(server_id):
        raise HTTPException(404, f"unknown MCP server: {server_id}")


@router.post("/api/mcp/servers/{server_id}/test")
def mcp_server_test(server_id: str) -> dict:
    """Connectivity check — list the server's tools via the MCP client."""
    from aiforge_core.config import mcp_registry
    from aiforge_core.runtime.tools import mcp_client
    row = mcp_registry.get_server(server_id)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    name = row.get("name") or row.get("id")
    return mcp_client.list_tools(name)


# ─────────────────────── graph_rag MCP tool call ───────────────────────────
_MCP_ALLOWED_TOOLS = {
    "sym_lookup", "list_repos", "list_services", "list_endpoints",
    "list_integrations", "graph_neighborhood", "caller_chain",
    "callee_chain", "read_source", "impact", "cross_repo_flow",
    "data_lineage", "build_plan", "test_plan", "kube_status",
    "kube_describe", "kube_image_tag", "kube_config", "find_doc",
    "related_memories", "ticket_fetch", "ticket_brief",
}


class _McpCallBody(BaseModel):
    tool: str = Field(..., description="Tool name from graph_rag MCP allowlist")
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/mcp/tool")
async def mcp_tool_call(body: _McpCallBody) -> dict:
    if body.tool not in _MCP_ALLOWED_TOOLS:
        raise HTTPException(400, f"tool '{body.tool}' not in allowlist")
    cmd = [
        os.environ.get("AIFORGE_MCP_BIN",
                       "aiforge-graph-mcp"),
    ]
    env = {
        **os.environ,
        "AIFORGE_NEO4J_URI": os.environ.get(
            "AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "AIFORGE_NEO4J_USER": os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
        "AIFORGE_NEO4J_PASSWORD": os.environ.get(
            "AIFORGE_NEO4J_PASSWORD", "password"),
        # graph_rag/cypher_lib reads NEO4J_URI / NEO4J_USER / NEO4J_PASS
        # (no AIFORGE_ prefix); mirror so the subprocess can connect.
        "NEO4J_URI": os.environ.get(
            "AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        "NEO4J_USER": os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
        "NEO4J_PASS": os.environ.get(
            "AIFORGE_NEO4J_PASSWORD", "password"),
        # Embed sidecar — graph_mcp defaults to :1235/v1 (planner LLM
        # port) which 404s. Force the real sidecar URL for this run.
        "EMBED_URL": os.environ.get(
            "EMBED_URL", "http://127.0.0.1:8764"),
        # No baked-in credential: see config.env.default_pg_dsn. The literal
        # this replaced shipped a working password in source control.
        "AIFORGE_DSN": os.environ.get("AIFORGE_DSN") or _default_dsn(),
    }

    # JSON-RPC dance: initialize → tools/call → shutdown.
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "clientInfo": {"name": "aiforge-ui",
                                          "version": "0.1"}}}
    init_notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    tool_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": body.tool, "arguments": body.args}}
    payload = (
        json.dumps(init_req) + "\n" +
        json.dumps(init_notify) + "\n" +
        json.dumps(tool_req) + "\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(payload.encode()), timeout=30,
        )
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()   # reap — don't leak a zombie
        except Exception: pass
        raise HTTPException(504, "MCP server timed out")
    except FileNotFoundError:
        # No MCP server binary installed (operator reset 2026-06-26). Fail
        # soft so the UI shows a clean empty state instead of a 500.
        return {"ok": False, "error": "MCP not configured",
                "detail": f"binary not found: {cmd[0]}"}

    # Scan stdout line by line for the JSON-RPC response to id=2.
    result: dict | None = None
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            result = msg
            break
    if result is None:
        raise HTTPException(
            500, f"MCP call produced no response. stderr={err[:400]!r}",
        )
    if "error" in result:
        raise HTTPException(400, f"MCP error: {result['error']}")
    return {"tool": body.tool, "result": result.get("result")}


__all__ = ["router"]
