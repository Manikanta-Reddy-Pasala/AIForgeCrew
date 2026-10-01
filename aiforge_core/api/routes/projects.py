"""Chat projects and their memory (/api/projects, /api/memory/projects).

A project is a folder under the mounted repos root. The Projects page lists
them; opening one registers it so its memory brief is mirrored into
``<repo>/.aiforge/memory/MEMORY.md``. The memory routes are the per-project
management surface: read, edit, compact, move to global, stale facts, forget.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class _TextBody(BaseModel):
    text: str = Field(..., description="markdown")


class _StaleBody(BaseModel):
    fact: str = Field(..., min_length=1)
    action: str = Field("restore", description="restore | delete")


def _chat_stats() -> dict:
    """``{project name: {chats, last_activity}}`` from the chat store."""
    from aiforge_core.memory import projects
    from aiforge_core.runtime import chat_store
    out: dict = {}
    for s in chat_store.list_sessions() or []:
        name = projects.project_of(s.get("cwd"))
        if not name:
            continue
        row = out.setdefault(name, {"chats": 0, "last_activity": ""})
        row["chats"] += 1
        row["last_activity"] = max(row["last_activity"],
                                   str(s.get("updated_at") or ""))
    return out


@router.get("/api/projects")
def projects_list() -> dict:
    """The folders under the repos root, each with its chat and memory
    numbers."""
    import os

    from aiforge_core.memory import projects
    base = projects.root()
    stats = _chat_stats()
    rows = []
    for f in projects.list_folders():
        row = projects.summary(f)
        row.update(stats.get(f["name"], {"chats": 0, "last_activity": ""}))
        rows.append(row)
    return {"root": base, "exists": os.path.isdir(base), "projects": rows}


def _path_or_404(name: str) -> str:
    from aiforge_core.memory import projects
    path = projects.resolve(name)
    if not path:
        raise HTTPException(404, f"no project folder named {name!r}")
    return path


def _slug_or_404(name: str) -> str:
    """The registered project for ``name``; opens it on first use so a project
    that already has memory can be managed before any chat is started."""
    from aiforge_core.memory import projects
    ent = projects.register(_path_or_404(name))
    if not ent:
        raise HTTPException(404, f"{name!r} cannot be a project")
    return ent["slug"]


@router.post("/api/projects/{name}/open")
def project_open(name: str) -> dict:
    """Register the project, bring its memory file into step, and start the
    one-time read of its instruction files."""
    from aiforge_core.memory import projects
    path = _path_or_404(name)
    ent = projects.open_project(path)
    if not ent:
        raise HTTPException(404, f"{name!r} cannot be a project")
    folder = next((f for f in projects.list_folders() if f["name"] == name),
                  {"name": name, "path": path, "is_git": False,
                   "has_aiforge": False})
    return projects.summary(folder)


@router.get("/api/memory/projects")
def memory_projects() -> list[dict]:
    """Projects that have memory or have been opened."""
    from aiforge_core.memory import projects
    return [r for r in (projects.summary(f) for f in projects.list_folders())
            if r["registered"] or r["memory_chars"]]


@router.get("/api/memory/projects/{name}")
def memory_project_get(name: str) -> dict:
    from aiforge_core.memory import projects
    return projects.read(_slug_or_404(name)) or {}


@router.put("/api/memory/projects/{name}", responses={400: {"description": "Bad request"}})
def memory_project_put(name: str, body: _TextBody) -> dict:
    from aiforge_core.memory import projects
    res = projects.save(_slug_or_404(name), body.text)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "save failed"))
    return res


@router.post("/api/memory/projects/{name}/compact")
def memory_project_compact(name: str, force: bool = True) -> dict:
    """Sweep stale facts and fold the brief into a shorter one. The previous
    version is archived."""
    from aiforge_core.memory import projects
    return projects.compact(_slug_or_404(name), force=force)


@router.post("/api/memory/projects/{name}/promote", responses={400: {"description": "Bad request"}})
def memory_project_promote(name: str, body: _TextBody) -> dict:
    """Move the given lines from this project's memory into global memory."""
    from aiforge_core.memory import projects
    res = projects.promote(_slug_or_404(name), body.text)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "promote failed"))
    return res


@router.post("/api/memory/projects/{name}/stale", responses={400: {"description": "Bad request"}})
def memory_project_stale(name: str, body: _StaleBody) -> dict:
    """Restore a stale fact to the brief, or delete it for good."""
    from aiforge_core.memory import projects
    slug = _slug_or_404(name)
    fn = projects.stale_delete if body.action == "delete" else projects.stale_restore
    res = fn(slug, body.fact)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "failed"))
    return res


@router.delete("/api/memory/projects/{name}")
def memory_project_forget(name: str) -> dict:
    """Forget the project's memory. The brief is archived, not destroyed."""
    from aiforge_core.memory import projects
    return projects.forget(_slug_or_404(name))
