"""Minimal read-only UI for AiForgeMemory.

Single-file FastAPI server + embedded HTML/JS. No auth — bind to
localhost or trusted LAN. Five things visible:

    /                  Dashboard (repos, scheduler, health)
    /api/repos         List repos + counts
    /api/health        Sidecar status (calls ops.health)
    /api/scheduler     Daemon + per-repo last_run/status
    /api/search        NL search → ContextBundle (any repo)
    /api/memory        Memory nodes (decision/observation/note/doc)
    /api/repo/{name}   Repo detail (recent files + summaries)
    /api/file          File detail by path (chunks + symbols)

Run:
    aiforge-memory ui [--host 0.0.0.0] [--port 8767]

Requires fastapi + uvicorn (added to optional `[ui]` extra).

This module was split (grouped by concern) into ``_graphify`` (graphify-out
discovery helpers) and ``_server`` (FastAPI app factory + entrypoint)
submodules; this package re-exports the full former public surface so
``from aiforge_memory.ui import server`` and every ``server.<name>``
attribute access is unchanged.
"""
from __future__ import annotations

from ._graphify import (
    _graphify_extra_roots,
    _graphify_index,
    _graphify_metadata,
    _resolve_graphify_path,
)
from ._server import (
    _GRAPHIFY_JSON_CAP_BYTES,
    _HAS_FASTAPI,
    HTML_PATH,
    build_app,
    serve,
)

__all__ = [
    "HTML_PATH",
    "build_app",
    "serve",
]
