"""Repo-folder mapping routes (/api/repos) — split out of api.py.

Self-contained: only the config.repo_map module + the request body. Behavior
identical to the inline handlers.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
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


@router.put("/api/repos", responses={400: {"description": "Bad request"}})
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


@router.delete("/api/repos/{name}", responses={404: {"description": "Not found"}})
def repos_delete(name: str) -> dict:
    """Remove an explicit per-repo path mapping."""
    from aiforge_core.config import repo_map
    r = repo_map.delete_path(name)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "not found"))
    return repo_map.list_all()


# ─────────────────────────── Repo standards ────────────────────────────
@router.get("/api/repo/standards")
def repo_standards_get(
    name: str = Query(..., description="Repo name (matches :Repo.name)"),
    worktree: str | None = None,
) -> dict:
    """Resolved per-project standards (commands + conventions)."""
    from aiforge_core.runtime import repo_standards as _rs
    std = _rs.get(name, worktree=worktree)
    return {
        "name": std.name, "lang": std.lang, "stack": std.stack,
        "ports": std.ports, "dockerfile": std.dockerfile,
        "entry_cmd": std.entry_cmd, "build_cmd": std.build_cmd,
        "compile_cmd": std.compile_cmd, "test_cmd": std.test_cmd,
        "lint_cmd": std.lint_cmd, "format_cmd": std.format_cmd,
        "security_scan_cmd": std.security_scan_cmd,
        "conventions": std.conventions,
        "forbidden_patterns": std.forbidden_patterns,
        "env_vars": std.env_vars,
        "acceptance_criteria": std.acceptance_criteria,
        "source": std.source,
    }


class _StandardsBody(BaseModel):
    build_cmd: str | None = None
    compile_cmd: str | None = None
    test_cmd: str | None = None
    lint_cmd: str | None = None
    format_cmd: str | None = None
    security_scan_cmd: str | None = None
    entry_cmd: str | None = None
    conventions: list[str] | None = None
    forbidden_patterns: list[str] | None = None
    env_vars: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    lang: str | None = None
    stack: list[str] | None = None
    ports: list[int] | None = None


@router.put("/api/repo/standards/{name}")
def repo_standards_set(name: str, body: _StandardsBody) -> dict:
    """Persist standards onto the Neo4j ``:Repo`` node."""
    from aiforge_core.runtime import repo_standards as _rs
    _rs.upsert(name, **{k: v for k, v in body.model_dump().items()
                        if v is not None})
    return repo_standards_get(name=name)
