"""OpenHands-parity AgentSkills helper library (sub #12).

Convenience functions auto-injected into the persistent IPython kernel
on first ``execute_ipython_cell`` call. Mirrors OH's `agentskills`
package: small, opinionated wrappers around the editor / bash / search
surface so the model can call ``open_file('foo.py', 100, 50)`` instead
of building an editor view-range every time.

These run INSIDE the kernel process so they share variables with user
code. The bootstrap source is materialised into a temp module and
``%load`` 'd into the kernel namespace.
"""
from __future__ import annotations


BOOTSTRAP_SOURCE = '''
"""AgentSkills — auto-loaded helpers (do not call directly outside the kernel)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    raw = os.environ.get("AIFORGE_REPO_ROOT")
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def open_file(path: str, line: int = 1, context: int = 50) -> str:
    """Print ``2*context`` lines around ``line`` of ``path`` and return the slice."""
    p = (_repo_root() / path).resolve()
    if not p.is_file():
        return f"<not_found: {path}>"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(0, line - 1 - context)
    end = min(len(lines), line - 1 + context)
    block = []
    for i in range(start, end):
        marker = ">>" if (i + 1) == line else "  "
        block.append(f"{marker} {i + 1:4d}  {lines[i]}")
    out = "\\n".join(block)
    print(out)
    return out


def goto_line(path: str, line: int) -> str:
    """Alias for ``open_file(path, line)`` — pinpoint context window."""
    return open_file(path, line=line)


def find_file(filename: str, root: str = ".") -> list[str]:
    """Return repo-relative paths whose basename matches ``filename``."""
    base = (_repo_root() / root).resolve()
    hits: list[str] = []
    for p in base.rglob(filename):
        try:
            rel = str(p.relative_to(_repo_root()))
        except ValueError:
            rel = str(p)
        hits.append(rel)
        if len(hits) >= 50:
            break
    return hits


def search_dir(pattern: str, root: str = ".") -> list[dict]:
    """Recursive regex search; returns up to 50 matches."""
    base = (_repo_root() / root).resolve()
    rx = re.compile(pattern)
    out: list[dict] = []
    for p in base.rglob("*"):
        if not p.is_file() or any(skip in p.parts for skip in (
            ".git", "node_modules", ".venv", "__pycache__", "target", "build",
        )):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                try:
                    rel = str(p.relative_to(_repo_root()))
                except ValueError:
                    rel = str(p)
                out.append({"file": rel, "line": i, "text": line[:200]})
                if len(out) >= 50:
                    return out
    return out


def search_file(pattern: str, path: str) -> list[dict]:
    """Regex search within a single file. Returns ``[{line, text}]``."""
    p = (_repo_root() / path).resolve()
    if not p.is_file():
        return []
    rx = re.compile(pattern)
    hits: list[dict] = []
    for i, line in enumerate(
        p.read_text(encoding="utf-8", errors="replace").splitlines(), 1,
    ):
        if rx.search(line):
            hits.append({"line": i, "text": line[:200]})
    return hits


def create_file(path: str, content: str = "") -> str:
    """Create a new file relative to repo root. Returns the resolved path
    or an ``<error>`` marker."""
    p = (_repo_root() / path).resolve()
    if p.exists():
        return f"<exists: {path}>"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return str(p)


def run_cmd(cmd: str, timeout: int = 60) -> dict:
    """Run a shell command and return ``{ok, stdout, stderr, returncode}``."""
    # Honour the same delete policy as the bash tool — this kernel-injected
    # helper must not be a bypass for the confirm-before-delete guard.
    try:
        from aiforge_core.runtime.tools import delete_guard
        if not delete_guard.allow_delete() \
                and delete_guard.is_destructive_delete(cmd):
            return {"ok": False, "blocked": "delete", "error": delete_guard.REFUSAL}
    except Exception:  # noqa: BLE001
        pass
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=_repo_root(),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace")[:8000],
        "stderr": proc.stderr.decode("utf-8", "replace")[:8000],
    }


__all__ = [
    "open_file", "goto_line", "find_file",
    "search_dir", "search_file", "create_file", "run_cmd",
]
'''


def bootstrap_code() -> str:
    """Return the source string that should be exec'd in the kernel
    namespace on first ``execute_ipython_cell`` call."""
    return BOOTSTRAP_SOURCE


__all__ = ["bootstrap_code", "BOOTSTRAP_SOURCE"]
