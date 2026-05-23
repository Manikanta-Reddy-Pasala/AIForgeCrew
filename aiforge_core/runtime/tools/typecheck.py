"""Type-check tool (standards gap C4).

KISS: one function probes the worktree for a language marker, runs
the canonical type-checker, returns a structured verdict the Feedback
agent can grade against.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from aiforge_core.runtime.sandbox import root

log = logging.getLogger("aiforge.tools.typecheck")

# (marker_file, command, language label). Order matters: first match wins.
_DETECTORS: list[tuple[str, list[str], str]] = [
    ("pyproject.toml", ["mypy", "--ignore-missing-imports", "."], "python"),
    ("setup.py",       ["mypy", "--ignore-missing-imports", "."], "python"),
    ("tsconfig.json",  ["npx", "tsc", "--noEmit"],                "typescript"),
    ("go.mod",         ["go", "build", "./..."],                   "go"),
    ("Cargo.toml",     ["cargo", "check", "--message-format=short"], "rust"),
    ("pom.xml",        ["mvn", "-q", "-DskipTests", "compile"],     "java-maven"),
    ("build.gradle",   ["./gradlew", "compileJava"],                "java-gradle"),
]


def typecheck() -> dict[str, Any]:
    """Run the worktree's canonical type-checker.

    Returns ``{ok, language, exit_code, stdout, stderr}``. ``ok`` is
    True only when the underlying tool exits 0 AND we successfully
    detected a language.

    No language marker found → ``{ok: False, error: "no_language"}``.
    Tool missing from PATH → ``{ok: False, error: "missing_tool"}``.
    """
    repo = root()
    for marker, cmd, lang in _DETECTORS:
        if not (repo / marker).is_file() and \
                not list(repo.glob(f"**/{marker}"))[:1]:
            continue
        tool = cmd[0]
        if shutil.which(tool) is None:
            return {"ok": False, "error": "missing_tool", "tool": tool,
                    "language": lang}
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
                cwd=str(repo),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "language": lang}
        return {
            "ok": p.returncode == 0,
            "language": lang,
            "exit_code": p.returncode,
            "stdout": p.stdout[-4000:],
            "stderr": p.stderr[-4000:],
        }
    return {"ok": False, "error": "no_language"}


__all__ = ["typecheck"]
