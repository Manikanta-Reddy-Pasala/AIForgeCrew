"""Lint loop — run a lint command, parse errors, feed back to model.

Aider's `--lint-cmd` analogue. Configurable via:
  AIFORGE_DOER_LINT_CMD       — full shell command (default: empty = disabled)
  AIFORGE_DOER_LINT_AUTO      — '1' to run automatically post-edit (default 0)

Recipe per repo can override via ``.aiforge/aiforge.conf.yml``
(see ``repo_config``); env wins for hot-swapping.

Returns the lint command's tail output trimmed to ~4 KB so the
model sees specific filenames + line numbers without flooding
context.
"""
from __future__ import annotations

import os
import shlex
import subprocess

SCHEMA = {
    "type": "function",
    "function": {
        "name": "lint",
        "description": (
            "Run the configured lint command (e.g. mvn checkstyle:check, "
            "javac -Xlint, pylint) inside the worktree and return its "
            "tail. Use AFTER patches land + compile is green to catch "
            "style / unused-import / Lombok-misuse before publishing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command. Empty → uses "
                        "AIFORGE_DOER_LINT_CMD env or the repo's "
                        "aiforge.conf.yml lint_cmd. Default for "
                        "Java repos: 'mvn -DskipTests "
                        "checkstyle:check'."
                    ),
                },
            },
        },
    },
}


_DEFAULT_LINT_CMD_BY_LANG = {
    "java": "mvn -DskipTests checkstyle:check",
    "python": "ruff check .",
    "ts": "npx tsc --noEmit",
}


def _resolve_command(worktree: str, override: str) -> str:
    if override:
        return override
    env = (
        os.environ.get("AIFORGE_DOER_LINT_CMD")
        or os.environ.get("AIFORGE_LINT_CMD")
    )
    if env:
        return env
    # Centralised standards catalogue (Neo4j :Repo + worktree YAML).
    try:
        from aiforge_core.runtime import repo_standards as _rs
        repo_name = os.path.basename(os.path.normpath(worktree))
        std = _rs.get(repo_name, worktree=worktree)
        if std.lint_cmd:
            return std.lint_cmd
    except Exception:
        pass
    # Sniff worktree for known lang.
    if os.path.isfile(os.path.join(worktree, "pom.xml")):
        return _DEFAULT_LINT_CMD_BY_LANG["java"]
    if os.path.isfile(os.path.join(worktree, "pyproject.toml")):
        return _DEFAULT_LINT_CMD_BY_LANG["python"]
    if os.path.isfile(os.path.join(worktree, "tsconfig.json")):
        return _DEFAULT_LINT_CMD_BY_LANG["ts"]
    return ""


def handle(worktree: str, args: dict, *, timeout_s: int = 300) -> str:
    cmd = _resolve_command(worktree, (args.get("command") or "").strip())
    if not cmd:
        return ("[lint] no lint command configured. Set "
                "AIFORGE_DOER_LINT_CMD or pass `command`.")
    try:
        proc = subprocess.run(
            shlex.split(cmd) if not cmd.startswith("/") else cmd,
            cwd=worktree, capture_output=True, text=True,
            timeout=timeout_s, shell=False,
        )
    except subprocess.TimeoutExpired:
        return f"[lint] timed out after {timeout_s}s; cmd: {cmd}"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = "\n".join(out.splitlines()[-80:])[-4000:]
    status = "clean" if proc.returncode == 0 else f"violations rc={proc.returncode}"
    return f"[lint] {cmd!r} → {status}\n{tail}"
