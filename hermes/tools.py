"""Tool registry — permission-gated dispatch for Hermes agents.

Each tool has a name, JSONSchema for args, a handler, and a required capability
from `agents/<role>/permissions.yml`. Hermes calls `dispatch(role, name, args)`
and the registry enforces the ACL before invocation.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from paperclip.permissions import PermissionDenied, file_access, role_can


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict           # JSON schema for arguments
    handler: Callable[[dict], Any]
    capability: str | None = None     # role capability required (ticket_comment, etc.)


class ToolRegistry:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def list_for_role(self, role: str) -> list[Tool]:
        out: list[Tool] = []
        for t in self._tools.values():
            if t.capability is None or role_can(self.repo_root, role, t.capability):
                out.append(t)
        return out

    def openai_schema(self, role: str) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema,
                },
            }
            for t in self.list_for_role(role)
        ]

    def dispatch(self, role: str, name: str, args: dict) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        t = self._tools[name]
        if t.capability and not role_can(self.repo_root, role, t.capability):
            raise PermissionDenied(f"role={role} lacks {t.capability} for tool {name}")
        return t.handler(args)


# -------------------- Built-in tools --------------------

def _read_file(repo_root: Path, role: str, args: dict) -> dict:
    path = args["path"]
    if not file_access(repo_root, role, "read", path):
        raise PermissionDenied(f"role={role} cannot read {path}")
    p = (repo_root / path).resolve()
    if not str(p).startswith(str(repo_root.resolve())):
        raise PermissionDenied(f"path escape: {path}")
    return {"path": path, "content": p.read_text()}


def _write_file(repo_root: Path, role: str, args: dict) -> dict:
    path = args["path"]
    content = args["content"]
    if not file_access(repo_root, role, "write", path):
        raise PermissionDenied(f"role={role} cannot write {path}")
    p = (repo_root / path).resolve()
    if not str(p).startswith(str(repo_root.resolve())):
        raise PermissionDenied(f"path escape: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"path": path, "bytes": len(content)}


def _list_dir(repo_root: Path, role: str, args: dict) -> dict:
    path = args.get("path", ".")
    if not file_access(repo_root, role, "read", path):
        raise PermissionDenied(f"role={role} cannot read {path}")
    p = (repo_root / path).resolve()
    if not p.is_dir():
        raise FileNotFoundError(path)
    return {"path": path, "entries": sorted([x.name for x in p.iterdir()])}


def _run_tests(repo_root: Path, role: str, args: dict) -> dict:
    # Only tester/sr-developer can execute. Capability enforced by Tool.capability.
    test_path = args.get("path", "tests/")
    cmd = ["pytest", test_path, "-q"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_root), timeout=300)
        return {"exit": r.returncode, "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": "", "stderr": "timeout"}


def build_default_registry(repo_root: Path, role: str) -> ToolRegistry:
    """Register the P2 baseline tool set for a given role; permission checks are later enforced per call."""
    reg = ToolRegistry(repo_root)

    reg.register(Tool(
        name="read_file",
        description="Read a UTF-8 text file from the repo. Path is repo-relative.",
        schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=lambda a: _read_file(repo_root, role, a),
        capability=None,  # file_access() gate used instead of coarse capability
    ))
    reg.register(Tool(
        name="write_file",
        description="Write text to a repo-relative file. Creates parent dirs.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        handler=lambda a: _write_file(repo_root, role, a),
        capability=None,
    ))
    reg.register(Tool(
        name="list_dir",
        description="List entries in a repo-relative directory.",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda a: _list_dir(repo_root, role, a),
        capability=None,
    ))
    reg.register(Tool(
        name="run_tests",
        description="Run pytest against a given path (default tests/). Only tester + sr-developer.",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda a: _run_tests(repo_root, role, a),
        capability="hermes_execute",
    ))
    return reg
