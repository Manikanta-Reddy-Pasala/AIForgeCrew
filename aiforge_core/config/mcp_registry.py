"""MCP server marketplace + installed-server registry (Cline-parity).

Two halves:

  1. **Catalog** — a curated, version-controlled list of MCP servers shipped
     with the repo (``mcp_catalog.json``), so the user can browse + one-click
     install instead of hand-editing ``AIFORGE_MCP_ENDPOINTS``.
  2. **Installed registry** — the servers the user has installed/enabled,
     stored as JSON at ``$AIFORGE_CONFIG_DIR/security/mcp_servers.json`` (mirrors
     ``model_registry.py``). API keys/headers are kept server-side and never
     returned (only ``api_key_set``).

Both HTTP/SSE and stdio (local ``command``+``args``) transports are installable
end-to-end. HTTP/SSE talks to a hosted MCP server; stdio spawns a LOCAL child
process (e.g. ``npx @modelcontextprotocol/server-filesystem``) — no network,
no phone-home. stdio servers carry ``{command, args, env}`` instead of a url.

``enabled_endpoints()`` returns the ``{name: url}`` map (HTTP/SSE only) the
agent's ``mcp_client`` merges on top of the env CSV; ``enabled_stdio_servers()``
returns the parallel ``{name: {command, args, env}}`` map for local stdio
servers. Either makes an installed+enabled server callable by the Doer.
"""
from __future__ import annotations

import json
import os
import re
import threading

from aiforge_core.config import _atomic

_LOCK = threading.Lock()
_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "mcp_catalog.json")


def _path() -> str:
    """Registry rows carry api keys and auth headers, so the file lives in the
    0700 ``security/`` folder (``config.secure_store``) like every other
    credential; a legacy copy in the config root moves there on first use."""
    from aiforge_core.config.secure_store import secure_path
    return str(secure_path("mcp_servers.json"))


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return []


def _save(rows: list[dict]) -> None:
    _atomic.write_text(_path(), json.dumps(rows, indent=2))


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "mcp").lower()).strip("-")
    return base or "mcp"


def _is_http(transport: str, url: str) -> bool:
    from aiforge_core.net.url_policy import is_allowed
    return (transport or "http").lower() in ("http", "sse") \
        and is_allowed(url)


def _is_stdio(transport: str) -> bool:
    return (transport or "http").lower() == "stdio"


def load_catalog() -> list[dict]:
    """The curated marketplace catalog. Each entry is annotated with
    ``installable`` (HTTP/SSE only today)."""
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        transport = (r.get("transport") or "http").lower()
        # A "custom-http" template has an empty url but is still installable
        # (the user fills the url in after installing). stdio servers are
        # installable too — they spawn a LOCAL child process.
        installable = transport in ("http", "sse", "stdio")
        out.append({**r, "installable": installable})
    return out


def _public(row: dict) -> dict:
    """Registry row without the raw key/header/env secrets."""
    return {
        "id": row.get("id"),
        "name": row.get("name") or row.get("id"),
        "url": row.get("url") or "",
        "transport": row.get("transport") or "http",
        # stdio servers carry a local command; args are safe to surface, env
        # values are kept server-side (may hold secrets) — only the keys leak.
        "command": row.get("command") or "",
        "args": list(row.get("args") or []),
        "env_keys": sorted((row.get("env") or {}).keys()),
        "enabled": bool(row.get("enabled", True)),
        "catalog_id": row.get("catalog_id") or "",
        "description": row.get("description") or "",
        "api_key_set": bool(row.get("api_key")),
    }


def list_servers() -> list[dict]:
    return [_public(r) for r in _load()]


def get_server(server_id: str) -> dict | None:
    for r in _load():
        if r.get("id") == server_id:
            return r
    return None


def _validated_transport(transport: str, url: str, command: str, args, env):
    """(url, command, args, env) normalised for the transport, or ValueError.

    The two transports are mutually exclusive — an http server has no command,
    a stdio server has no url — so this returns the CLEARED fields rather than
    leaving each caller to remember which half to blank.
    """
    if transport in ("http", "sse"):
        from aiforge_core.net.url_policy import check
        why = check(url) if url else None
        if why:
            raise ValueError(why)
        return url, "", None, None
    if transport == "stdio":
        if not command:
            raise ValueError("stdio server requires a command (e.g. npx)")
        return "", command, args, env
    raise ValueError(f"transport not supported: {transport} (http/sse/stdio)")


def _unique_id(base: str, taken: set) -> str:
    """``base``, ``base-2``, ``base-3`` … — the first that is free."""
    uid, n = base, 2
    while uid in taken:
        uid = f"{base}-{n}"
        n += 1
    return uid


def add_server(*, name: str, url: str = "", transport: str = "http",
               api_key: str | None = None, description: str = "",
               catalog_id: str = "", enabled: bool = True,
               command: str = "", args: list | None = None,
               env: dict | None = None) -> dict:
    """Register an MCP server.

    HTTP/SSE servers need a ``url``; stdio servers need a local ``command``
    (with optional ``args``/``env``) — they spawn a LOCAL child process.
    Raises ValueError on an unsupported transport, a non-http url for an
    http server, or a stdio server with no command."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    transport = (transport or "http").lower()
    command = (command or "").strip()
    url = (url or "").strip()
    url, command, args, env = _validated_transport(
        transport, url, command, args, env)
    with _LOCK:
        rows = _load()
        uid = _unique_id(_slug(name), {r["id"] for r in rows})
        row = {"id": uid, "name": name, "url": url, "transport": transport,
               "command": command, "args": list(args or []),
               "env": dict(env or {}),
               "api_key": api_key or "", "description": (description or "").strip(),
               "catalog_id": (catalog_id or "").strip(), "enabled": bool(enabled)}
        rows.append(row)
        _save(rows)
        return _public(row)


def install_from_catalog(catalog_id: str, *, url: str | None = None,
                         api_key: str | None = None,
                         name: str | None = None) -> dict:
    """Install a catalog entry as a registered server. ``url``/``name``
    override the catalog defaults (needed for the custom template)."""
    entry = next((c for c in load_catalog() if c.get("id") == catalog_id), None)
    if entry is None:
        raise ValueError(f"unknown catalog entry: {catalog_id}")
    if not entry.get("installable"):
        raise ValueError(f"{catalog_id} is not installable")
    transport = (entry.get("transport") or "http").lower()
    if transport == "stdio":
        # LOCAL stdio server — spawn a child process, no url involved.
        return add_server(
            name=name or entry.get("name") or catalog_id,
            transport="stdio", command=entry.get("command") or "",
            args=entry.get("args") or [], env=entry.get("env") or {},
            description=entry.get("description") or "",
            catalog_id=catalog_id, enabled=True)
    final_url = (url if url is not None else entry.get("url")) or ""
    if not final_url:
        raise ValueError("a url is required for this server")
    return add_server(
        name=name or entry.get("name") or catalog_id,
        url=final_url, transport=transport,
        api_key=api_key, description=entry.get("description") or "",
        catalog_id=catalog_id, enabled=True)


# How each updatable field is coerced. A table, because the old chain of ifs
# was the same shape nine times over and the only real content was the coercion.
_UPDATABLE = {
    "name": lambda v: str(v).strip(),
    "url": lambda v: str(v).strip(),
    "description": lambda v: str(v).strip(),
    "command": lambda v: str(v).strip(),
    "args": list,
    "env": dict,
    "enabled": bool,
}


def _apply_updates(row: dict, fields: dict) -> None:
    """Patch ``row`` in place with the fields that were actually supplied."""
    for key, coerce in _UPDATABLE.items():
        if fields.get(key) is not None:
            row[key] = coerce(fields[key])
    # api_key is the exception: only a NON-EMPTY value overwrites, so that
    # saving a form that renders the key blank cannot silently erase it.
    if fields.get("api_key"):
        row["api_key"] = fields["api_key"]


def update_server(server_id: str, **fields) -> dict | None:
    with _LOCK:
        rows = _load()
        row = next((r for r in rows if r.get("id") == server_id), None)
        if row is None:
            return None
        _apply_updates(row, fields)
        _save(rows)
        return _public(row)


def remove_server(server_id: str) -> bool:
    with _LOCK:
        rows = _load()
        new = [r for r in rows if r.get("id") != server_id]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def enabled_endpoints() -> dict[str, str]:
    """``{name: url}`` for every enabled HTTP/SSE server — merged into the
    agent's ``mcp_client`` endpoint map. stdio/empty-url rows are skipped."""
    out: dict[str, str] = {}
    for r in _load():
        if not r.get("enabled", True):
            continue
        url = r.get("url") or ""
        if _is_http(r.get("transport") or "http", url):
            out[r.get("name") or r.get("id")] = url
    return out


def enabled_stdio_servers() -> dict[str, dict]:
    """``{name: {transport, command, args, env}}`` for every enabled LOCAL
    stdio server — merged into the agent's ``mcp_client`` as a parallel map to
    the HTTP endpoints. HTTP/empty-command rows are skipped."""
    out: dict[str, dict] = {}
    for r in _load():
        if not r.get("enabled", True):
            continue
        if _is_stdio(r.get("transport") or "http") and r.get("command"):
            out[r.get("name") or r.get("id")] = {
                "transport": "stdio",
                "command": r.get("command"),
                "args": list(r.get("args") or []),
                "env": dict(r.get("env") or {}),
            }
    return out


__all__ = ["load_catalog", "list_servers", "get_server", "add_server",
           "install_from_catalog", "update_server", "remove_server",
           "enabled_endpoints", "enabled_stdio_servers"]
