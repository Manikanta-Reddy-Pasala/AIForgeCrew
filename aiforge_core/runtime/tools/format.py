"""Format tool (standards gap C3).

Single function: detect language by file suffix, run the project's
canonical formatter. KISS — no config file, no plugin system. Adding
a new language = one entry in ``_FORMATTERS``.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root, root  # noqa: F401

log = logging.getLogger("aiforge.tools.format")

# Map suffix → command template. ``{path}`` placeholder is the file
# path relative to the worktree. Each entry is a list (no shell=True)
# so paths with spaces don't break.
_FORMATTERS: dict[str, list[str]] = {
    ".py": ["ruff", "format", "{path}"],
    ".js": ["prettier", "--write", "{path}"],
    ".jsx": ["prettier", "--write", "{path}"],
    ".ts": ["prettier", "--write", "{path}"],
    ".tsx": ["prettier", "--write", "{path}"],
    ".json": ["prettier", "--write", "{path}"],
    ".md": ["prettier", "--write", "{path}"],
    ".go": ["gofmt", "-w", "{path}"],
    ".rs": ["rustfmt", "{path}"],
    ".java": ["google-java-format", "-i", "{path}"],
}


def format(path: str) -> dict[str, Any]:
    """Format ``path`` in place using the project's canonical
    formatter. Returns ``{ok, formatter, stdout, stderr}``.

    Args:
        path: file path relative to ``AIFORGE_REPO_ROOT``. The runner
            pins this to the per-ticket worktree so edits land where
            git_pr expects them.

    Behaviour:
        - Unknown suffix → ``{ok: False, error: "unsupported"}``.
        - Formatter not on PATH → ``{ok: False, error: "missing_tool"}``.
            We don't auto-install — surfaces the missing dependency to
            the operator instead of silently changing the env.
        - Tool exit 0 → ``{ok: True}``.
        - Non-zero exit → ``{ok: False, stderr, exit_code}``.
    """
    rel = (path or "").strip()
    if not rel:
        return {"ok": False, "error": "empty_path"}
    # Containment: a formatter run with -w would rewrite a file OUTSIDE the
    # worktree if the path escaped (../../). resolve_inside_root rejects that.
    try:
        abs_path = resolve_inside_root(rel)
    except PermissionError:
        return {"ok": False, "error": "path_outside_root", "path": rel}
    suffix = abs_path.suffix.lower()
    cmd_tmpl = _FORMATTERS.get(suffix)
    if not cmd_tmpl:
        return {"ok": False, "error": "unsupported", "suffix": suffix}
    tool_name = cmd_tmpl[0]
    if shutil.which(tool_name) is None:
        return {"ok": False, "error": "missing_tool", "tool": tool_name}
    cmd = [str(abs_path) if part == "{path}" else part for part in cmd_tmpl]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(root()),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "tool": tool_name}
    return {
        "ok": p.returncode == 0,
        "formatter": tool_name,
        "stdout": p.stdout[-2000:],
        "stderr": p.stderr[-2000:],
        "exit_code": p.returncode,
    }


__all__ = ["format"]
