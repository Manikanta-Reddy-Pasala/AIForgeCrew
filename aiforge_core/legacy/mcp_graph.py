"""Load the graph_rag MCP server's 25 tools into smolagents.

Enabled by setting `AIFORGE_GRAPH_MCP_ENABLED=1` in the process env.
Transport is stdio over SSH to the NUC venv. The connection is opened
once per process and cached — smolagents agents can append the returned
list to their own tool list.

Design:
- Opt-in — agents that don't need graph tools don't pay the SSH cost.
- Cached — the ToolCollection context manager is held open until process
  exit so multiple ticks reuse the same MCP session.
- Graceful fallback — any exception yields an empty list + log line.
"""
from __future__ import annotations

import atexit
import logging
import os
from functools import lru_cache
from typing import Any

log = logging.getLogger(__name__)

_MCP_CTX = None  # smolagents.ToolCollection context manager instance
_MCP_TOOLS: list[Any] = []


def _build_command() -> tuple[str, list[str], dict]:
    """Return (command, args, env) for MCP stdio transport.

    - If `AIFORGE_GRAPH_MCP_BIN` is set, run that binary directly (local —
      preferred when the process is already on the NUC).
    - Otherwise SSH to `AIFORGE_GRAPH_MCP_HOST` (default mani@NUC) and run
      the aiforge-graph-mcp binary there.
    """
    local_bin = os.environ.get("AIFORGE_GRAPH_MCP_BIN")
    # Env forwarded to the MCP server process. Defaults match the launched
    # server in both local and SSH modes.
    mcp_env = {
        "NEO4J_URI": os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
        "NEO4J_USER": os.environ.get("NEO4J_USER", "neo4j"),
        "NEO4J_PASS": os.environ.get("NEO4J_PASS", "password"),
        "EMBED_URL": os.environ.get("EMBED_URL", "http://127.0.0.1:1235/v1"),
        "EMBED_MODEL": os.environ.get(
            "EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5"),
        "LLM_URL": os.environ.get("LLM_URL", "http://127.0.0.1:1235/v1"),
        "LLM_MODEL": os.environ.get("LLM_MODEL", "qwen3-coder-next"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if local_bin:
        return local_bin, [], mcp_env
    target = os.environ.get("AIFORGE_GRAPH_MCP_HOST", "mani@192.168.70.191")
    remote_bin = os.environ.get(
        "AIFORGE_GRAPH_MCP_CMD",
        "/home/mani/aiforge-venv/bin/aiforge-graph-mcp")
    env_prefix = " ".join(f"{k}={v}" for k, v in mcp_env.items()
                          if k != "PATH")
    return "ssh", [target, f"{env_prefix} {remote_bin}"], os.environ.copy()


@lru_cache(maxsize=1)
def graph_rag_tools() -> list[Any]:
    """Return smolagents Tool objects for the graph_rag MCP server.

    Returns an empty list if disabled, smolagents lacks MCP support, or
    connection fails — callers can unconditionally `tools.extend(...)`.
    """
    global _MCP_CTX, _MCP_TOOLS
    if os.environ.get("AIFORGE_GRAPH_MCP_ENABLED") not in ("1", "true", "yes"):
        return []
    try:
        from smolagents import ToolCollection
        from mcp import StdioServerParameters
    except Exception as exc:
        log.warning("graph_rag MCP unavailable: %s", exc)
        return []

    try:
        cmd, args, env = _build_command()
        params = StdioServerParameters(command=cmd, args=args, env=env)
        ctx = ToolCollection.from_mcp(params, trust_remote_code=True)
        collection = ctx.__enter__()
        _MCP_CTX = ctx
        _MCP_TOOLS = list(collection.tools)
        atexit.register(_close)
        log.info("graph_rag MCP wired: %d tools (transport=%s)",
                 len(_MCP_TOOLS), "local" if os.environ.get("AIFORGE_GRAPH_MCP_BIN") else "ssh")
        return _MCP_TOOLS
    except Exception as exc:
        log.warning("graph_rag MCP connect failed: %s", exc)
        return []


def _close() -> None:
    global _MCP_CTX
    if _MCP_CTX is not None:
        try:
            _MCP_CTX.__exit__(None, None, None)
        except Exception:
            pass
        _MCP_CTX = None
