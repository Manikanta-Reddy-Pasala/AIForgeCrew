"""Repo-folder mapping routes (/api/repos) — split out of api.py.

Self-contained: only the config.repo_map module + the request body. Behavior
identical to the inline handlers.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class _RepoMapBody(BaseModel):
    default_root: str | None = None   # global base folder for all repos
    name: str | None = None           # a repo name to map to an explicit path
    path: str | None = None           # its absolute local folder


@router.get("/api/repos")
def repos_get() -> dict:
    """Configured repo folders: the global base + explicit per-repo paths."""
    from aiforge_core.config import repo_map
    return repo_map.list_all()


@router.put("/api/repos")
def repos_set(body: _RepoMapBody) -> dict:
    """Set the global base folder and/or an explicit per-repo path."""
    from aiforge_core.config import repo_map
    if body.default_root:
        r = repo_map.set_default_root(body.default_root)
        if not r.get("ok"):
            raise HTTPException(400, r.get("error", "bad default_root"))
    if body.name and body.path:
        r = repo_map.set_path(body.name, body.path)
        if not r.get("ok"):
            raise HTTPException(400, r.get("error", "bad path mapping"))
    elif bool(body.name) != bool(body.path):
        raise HTTPException(400, "name and path must be given together")
    return repo_map.list_all()


@router.delete("/api/repos/{name}")
def repos_delete(name: str) -> dict:
    """Remove an explicit per-repo path mapping."""
    from aiforge_core.config import repo_map
    r = repo_map.delete_path(name)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "not found"))
    return repo_map.list_all()
