"""MCP marketplace/installer routes.
Extracted from api.py (behavior-preserving) — was split across two locations.
"""
from __future__ import annotations

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


@router.post("/api/mcp/servers", status_code=201, responses={400: {"description": "Bad request"}})
def mcp_server_install(body: _McpInstallBody) -> dict:
    from aiforge_core.config import mcp_registry
    try:
        return mcp_registry.install_from_catalog(
            body.catalog_id, url=body.url, api_key=body.api_key, name=body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.put("/api/mcp/servers/{server_id}", responses={404: {"description": "Not found"}})
def mcp_server_update(server_id: str, body: _McpUpdateBody) -> dict:
    from aiforge_core.config import mcp_registry
    row = mcp_registry.update_server(
        server_id, name=body.name, url=body.url, description=body.description,
        enabled=body.enabled, api_key=body.api_key)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    return row


@router.delete("/api/mcp/servers/{server_id}", status_code=204, responses={404: {"description": "Not found"}})
def mcp_server_delete(server_id: str) -> None:
    from aiforge_core.config import mcp_registry
    if not mcp_registry.remove_server(server_id):
        raise HTTPException(404, f"unknown MCP server: {server_id}")


@router.post("/api/mcp/servers/{server_id}/test", responses={404: {"description": "Not found"}})
def mcp_server_test(server_id: str) -> dict:
    """Connectivity check — list the server's tools via the MCP client."""
    from aiforge_core.config import mcp_registry
    from aiforge_core.runtime.tools import mcp_client
    row = mcp_registry.get_server(server_id)
    if row is None:
        raise HTTPException(404, f"unknown MCP server: {server_id}")
    name = row.get("name") or row.get("id")
    return mcp_client.list_tools(name)


__all__ = ["router"]
