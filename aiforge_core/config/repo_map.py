"""Persistent repo → local-folder mapping.

Answers two operator needs that were previously un-storable:

  1. **Global base folder** for all repos — ``default_root`` (falls back to the
     ``AIFORGE_WORKTREE_ROOT`` env, then ``~/codeRepo``). A ticket whose
     ``project`` is ``foo`` resolves to ``<default_root>/foo``.
  2. **Per-repo explicit path** — ``paths[name] = /abs/path`` for a repo that
     lives OUTSIDE the base folder. This wins over the base-folder guess.

Stored as JSON at ``$AIFORGE_CONFIG_DIR/repos.json`` (same config dir as
integrations). Set from chat ("use /x/y for repo foo", "the base folder for all
repos is /x") via the ``set_repo_folder`` / ``set_repo_root`` tools, or the API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    cfg = os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))
    return Path(cfg) / "repos.json"


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return {}


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")


def default_root() -> str:
    """Global base folder for all repos. Stored value wins, else the
    ``AIFORGE_WORKTREE_ROOT`` env (read live, not the frozen constant), else
    ``~/codeRepo``."""
    stored = (_load().get("default_root") or "").strip()
    if stored:
        return os.path.expanduser(stored)
    return os.path.expanduser(os.environ.get("AIFORGE_WORKTREE_ROOT", "~/codeRepo"))


def set_default_root(path: str) -> dict:
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "path required"}
    d = _load()
    d["default_root"] = os.path.expanduser(path)
    _save(d)
    return {"ok": True, "default_root": d["default_root"]}


def get_path(name: str) -> str | None:
    """Explicit folder for repo ``name`` (case-insensitive), or None."""
    if not name:
        return None
    paths = _load().get("paths") or {}
    if name in paths:
        return os.path.expanduser(paths[name])
    low = name.strip().lower()
    for k, v in paths.items():
        if k.lower() == low:
            return os.path.expanduser(v)
    return None


def set_path(name: str, path: str) -> dict:
    name = (name or "").strip()
    path = (path or "").strip()
    if not name or not path:
        return {"ok": False, "error": "name and path required"}
    d = _load()
    d.setdefault("paths", {})[name] = os.path.expanduser(path)
    _save(d)
    return {"ok": True, "name": name, "path": d["paths"][name]}


def delete_path(name: str) -> dict:
    d = _load()
    paths = d.get("paths") or {}
    if name in paths:
        del paths[name]
        _save(d)
        return {"ok": True, "removed": name}
    return {"ok": False, "error": f"no mapping for {name!r}"}


def list_all() -> dict:
    d = _load()
    return {"default_root": default_root(), "paths": d.get("paths") or {}}
