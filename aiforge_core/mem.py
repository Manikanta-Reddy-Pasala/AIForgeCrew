"""Two-tier MemPalace wrapper per DESIGN.md §6.

Project memory = shared across all agents (writers: em + sr-architect only).
Per-agent memory = isolated palace per role (writer: owner only).

Backed by MemPalace (local-first semantic search, ChromaDB default). Each palace
lives at `.aiforge/mem/<scope>/` where `<scope>` is either `project` or
`agent/<role>`. Writes go through `MemBus` which enforces the ACL before
shelling out to `mempalace mine`; reads call `mempalace search` / `wake-up`.

Optional dep: install with `uv pip install -e '.[mem]'` or `pip install mempalace`.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .permissions import PermissionDenied

# Writers of project memory (DESIGN.md §6.1).
PROJECT_WRITERS = {"em", "sr-architect"}
# All roles can read project memory + their own.
ALL_ROLES = {"em", "tester", "sr-developer", "sr-architect"}


@dataclass
class MemBus:
    """Two-tier memory bus.

    base_dir = `.aiforge/mem` (gitignored). Tree:
        <base_dir>/project/         (shared palace)
        <base_dir>/agent/<role>/    (one per role)
    """

    base_dir: Path
    mempalace_bin: str = "mempalace"

    def ensure_init(self) -> None:
        """Idempotent: init the project palace + one per role if missing."""
        for scope in ("project", *(f"agent/{r}" for r in ALL_ROLES)):
            p = self.base_dir / scope
            if not (p / "config.json").is_file():
                p.mkdir(parents=True, exist_ok=True)
                self._run(["--palace", str(p), "init", str(p), "--yes"], check=False)

    # ---------- writes (ACL-gated) ----------
    def remember(self, role: str, scope: str, text: str, title: str | None = None) -> None:
        """Add a memory.

        scope = "project" (shared) or "own" (role's own palace).
        ACL: project is writable only by em + sr-architect; own is writable only by owner.
        """
        self._assert_writer(role, scope)
        palace = self._scope_path(role, scope)
        # Mine expects a file on disk; write a transient markdown doc then mine it.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=str(self.base_dir)
        ) as f:
            if title:
                f.write(f"# {title}\n\n")
            f.write(text.rstrip() + "\n")
            tmp = Path(f.name)
        try:
            self._run(["--palace", str(palace), "mine", str(tmp)])
        finally:
            tmp.unlink(missing_ok=True)

    # ---------- reads ----------
    def search(self, role: str, query: str, *, scope: str = "auto", limit: int = 5) -> list[str]:
        """Search memory; returns raw hit strings (truncated)."""
        hits: list[str] = []
        palaces = self._search_palaces(role, scope)
        for palace in palaces:
            out = self._run(
                ["--palace", str(palace), "search", query, "--limit", str(limit)],
                check=False,
            )
            if out:
                hits.append(out.strip())
        return hits

    def wake_up(self, role: str, *, scope: str = "own") -> str:
        """Compact wake-up context (DESIGN.md §6.1: <8K token budget)."""
        palace = self._scope_path(role, scope)
        return self._run(["--palace", str(palace), "wake-up"], check=False) or ""

    # ---------- internals ----------
    def _assert_writer(self, role: str, scope: str) -> None:
        if scope == "project":
            if role not in PROJECT_WRITERS:
                raise PermissionDenied(f"role={role} cannot write project memory")
            return
        if scope == "own":
            if role not in ALL_ROLES:
                raise PermissionDenied(f"unknown role: {role}")
            return
        raise ValueError(f"scope must be 'project' or 'own', got {scope!r}")

    def _scope_path(self, role: str, scope: str) -> Path:
        if scope == "project":
            return self.base_dir / "project"
        if scope == "own":
            return self.base_dir / "agent" / role
        raise ValueError(f"unknown scope {scope!r}")

    def _search_palaces(self, role: str, scope: str) -> list[Path]:
        if scope == "auto":
            return [self.base_dir / "project", self.base_dir / "agent" / role]
        return [self._scope_path(role, scope)]

    def _run(self, args: list[str], *, check: bool = True) -> str:
        cmd = [self.mempalace_bin, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise RuntimeError(f"mempalace failed: {r.stderr.strip()}")
        return r.stdout
