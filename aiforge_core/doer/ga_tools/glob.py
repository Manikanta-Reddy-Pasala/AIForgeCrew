"""Glob tool — fast file pattern matching, mirrors Claude Code's Glob.

Doer can list files matching a glob pattern under the worktree
without spawning a code_run shell. Faster than ``find`` and
honours .gitignore via ripgrep. Falls back to Python ``pathlib``
when ripgrep isn't present.

Returns a newline-separated list of repo-relative paths, sorted by
mtime (newest first), capped at ``max_results``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCHEMA = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": (
            "Find files matching a glob pattern under the worktree. "
            "Use to locate source files quickly. "
            "Returns repo-relative paths newest-first, max 50. "
            "Examples: '**/*Controller.java', "
            "'src/main/java/**/PaymentIn*.java'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.java'.",
                },
                "max_results": {
                    "type": "integer",
                    "default": 50,
                    "description": "Cap; default 50.",
                },
            },
            "required": ["pattern"],
        },
    },
}


def _ripgrep_glob(worktree: str, pattern: str, cap: int) -> list[str]:
    """Use ripgrep --files -g <pattern>; honours .gitignore."""
    rg = shutil.which("rg")
    if not rg:
        return []
    proc = subprocess.run(
        [rg, "--files", "-g", pattern],
        cwd=worktree, capture_output=True, text=True, timeout=10,
    )
    paths = [p for p in proc.stdout.splitlines() if p]
    paths.sort(
        key=lambda p: -os.path.getmtime(os.path.join(worktree, p))
        if os.path.isfile(os.path.join(worktree, p)) else 0
    )
    return paths[:cap]


def _python_glob(worktree: str, pattern: str, cap: int) -> list[str]:
    """Fallback when ripgrep is missing; pathlib globbing."""
    root = Path(worktree)
    matches = sorted(
        root.glob(pattern),
        key=lambda p: -p.stat().st_mtime if p.is_file() else 0,
    )
    return [
        str(p.relative_to(root))
        for p in matches if p.is_file()
    ][:cap]


def handle(worktree: str, args: dict) -> str:
    """Return the formatted glob result for ``do_glob`` to yield.

    Pure logic, no GA dependencies — easy to unit-test.
    """
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "[glob] empty pattern"
    cap = int(args.get("max_results") or 50)
    cap = max(1, min(cap, 200))
    paths = _ripgrep_glob(worktree, pattern, cap)
    if not paths:
        paths = _python_glob(worktree, pattern, cap)
    if not paths:
        return f"[glob] no files match {pattern!r}"
    header = f"[glob] {pattern!r} → {len(paths)} match"
    if len(paths) == cap:
        header += " (truncated)"
    return header + "\n" + "\n".join(paths)
