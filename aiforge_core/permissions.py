"""Permission enforcement for Paperclip + Hermes.

Each role's `agents/<role>/permissions.yml` lists boolean capabilities.
`check()` raises PermissionDenied if the role lacks a capability.
File ACL checks (read/write under src|tests|secrets) are resolved against
`security/file-access-rules.yml` + `security/blocked-paths.yml`.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import yaml

from .config import load_permissions


class PermissionDenied(PermissionError):
    pass


def role_can(repo_root: Path, role: str, capability: str) -> bool:
    caps = load_permissions(repo_root, role)
    return bool(caps.get(capability, False))


def check(repo_root: Path, role: str, capability: str) -> None:
    if not role_can(repo_root, role, capability):
        raise PermissionDenied(f"role={role!r} lacks capability {capability!r}")


def _load_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text()) or {}


def file_access(repo_root: Path, role: str, op: str, path: str) -> bool:
    """Return True if `role` may `op` (read|write) `path` under repo_root.

    Layers (first decisive hit wins, deny by default):
      1. security/blocked-paths.yml → unconditional deny for any role.
      2. security/file-access-rules.yml → per-role write / read allow patterns.
    Paths are matched with fnmatch against the normalized relative path
    (always starting with a leading slash is stripped).
    """
    if op not in ("read", "write"):
        raise ValueError("op must be 'read' or 'write'")

    rel = path.lstrip("./").lstrip("/")

    blocked = _load_yaml(repo_root / "security" / "blocked-paths.yml")
    for pat in (blocked.get("globally_blocked") or blocked.get("blocked_paths") or []):
        if fnmatch(rel, pat):
            return False

    rules = _load_yaml(repo_root / "security" / "file-access-rules.yml")
    role_rules = (rules.get("roles") or {}).get(role) or {}
    allow = role_rules.get("write" if op == "write" else "read") or []
    deny = role_rules.get("deny") or []

    for pat in deny:
        if fnmatch(rel, pat):
            return False
    for pat in allow:
        if fnmatch(rel, pat):
            return True
    return False
