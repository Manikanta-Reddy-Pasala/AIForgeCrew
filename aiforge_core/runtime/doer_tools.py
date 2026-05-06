"""Filesystem + shell tools the Doer LlmAgent calls during the v6 ADK
pipeline. Wired into ``runtime.adk_runner`` as
``google.adk.tools.FunctionTool``.

Modules:

* :mod:`sandbox`     — ``AIFORGE_REPO_ROOT`` resolver + traversal guard
* :mod:`syntax_guard`— pre-commit syntax sniff (see ``file_write``)
* :mod:`memory_lookup_tool` — hybrid AiForgeMemory recall

Each tool returns a JSON-serialisable dict so ADK can persist the
result in session state. Failures return ``{ok: False, error}`` instead
of raising — keeps the agent loop alive while still surfacing the
problem to the model.
"""
from __future__ import annotations

import os
import subprocess

from .sandbox import resolve_inside_root, root
from .syntax_guard import validate_syntax
from .memory_lookup_tool import memory_lookup


def file_read(path: str) -> dict:
    """Read a UTF-8 text file relative to the repo root.

    Returns ``{ok, path, content, bytes}`` on success, or
    ``{ok: False, error}``.
    """
    try:
        p = resolve_inside_root(path)
        if not p.is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": path,
                "content": text, "bytes": len(text.encode("utf-8"))}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def file_write(path: str, content: str) -> dict:
    """Create or overwrite a UTF-8 text file relative to the repo root.

    Runs :func:`syntax_guard.validate_syntax` first; rejects + returns
    a hint string when the draft fails so the Doer can self-correct
    on the next turn instead of leaking corrupt output to disk. Set
    ``AIFORGE_DOER_SKIP_SYNTAX=1`` to bypass (debug only).
    """
    try:
        p = resolve_inside_root(path)
        if os.environ.get("AIFORGE_DOER_SKIP_SYNTAX", "0") not in ("1", "true"):
            ok, err = validate_syntax(path, content)
            if not ok:
                return {
                    "ok": False,
                    "error": f"syntax_invalid: {err}",
                    "hint": (
                        "fix the syntax and call file_write again; "
                        "or call memory_lookup if you need symbol info"
                    ),
                }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path,
                "bytes": len(content.encode("utf-8"))}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def file_patch(path: str, old_text: str, new_text: str) -> dict:
    """Replace the FIRST occurrence of ``old_text`` with ``new_text``.

    Failure modes: ``not_found`` (file missing), ``old_text_not_found``
    (no match), ``ambiguous_match`` (>1 occurrence — caller passes
    more context to disambiguate).
    """
    try:
        p = resolve_inside_root(path)
        if not p.is_file():
            return {"ok": False, "error": "not_found"}
        body = p.read_text(encoding="utf-8")
        count = body.count(old_text)
        if count == 0:
            return {"ok": False, "error": "old_text_not_found"}
        if count > 1:
            return {"ok": False, "error": "ambiguous_match",
                    "occurrences": count}
        p.write_text(body.replace(old_text, new_text, 1), encoding="utf-8")
        return {"ok": True, "path": path, "replaced": True}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def list_dir(path: str = "") -> dict:
    """List directory entries under the repo root."""
    try:
        p = resolve_inside_root(path) if path else root()
        if not p.is_dir():
            return {"ok": False, "error": f"not a dir: {path}"}
        entries = []
        for child in sorted(p.iterdir()):
            if child.is_dir():
                kind = "dir"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append({"name": child.name, "kind": kind})
        return {"ok": True, "path": path or ".", "entries": entries}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def run_shell(cmd: str) -> dict:
    """Run a shell command inside the repo root.

    Hard timeout 90s; output truncated to 8 KB per stream so a runaway
    test suite cannot blow up session state.
    """
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=root(),
            capture_output=True, timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "timeout",
                "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:8000],
                "stderr": (exc.stderr or b"").decode("utf-8", "replace")[:8000]}
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": out[:8000], "stderr": err[:8000],
        "truncated": len(out) > 8000 or len(err) > 8000,
    }


# ─── Hallucination-tolerant aliases ────────────────────────────────────
#
# Without these the Doer's "Tool 'read' not found" failure (observed in
# ONE-105) crashes the run. Each alias delegates to the canonical
# implementation so behaviour stays single-sourced; the model can use
# whichever common name it pattern-matches to without a redeploy.

def read(path: str) -> dict:
    """Alias for :func:`file_read`."""
    return file_read(path)


def write(path: str, content: str) -> dict:
    """Alias for :func:`file_write`."""
    return file_write(path, content)


def patch(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch`."""
    return file_patch(path, old_text, new_text)


def ls(path: str = "") -> dict:
    """Alias for :func:`list_dir`."""
    return list_dir(path)


def shell(cmd: str) -> dict:
    """Alias for :func:`run_shell`."""
    return run_shell(cmd)


def bash(cmd: str) -> dict:
    """Alias for :func:`run_shell`."""
    return run_shell(cmd)


# ─── ADK wiring ────────────────────────────────────────────────────────


def adk_function_tools() -> list:
    """Return the Doer's tool list as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.

    Order — canonical names first so they show up at the top of the
    schema dump the model consumes; aliases follow as escape hatches.
    """
    from google.adk.tools import FunctionTool
    canonical = [file_read, file_write, file_patch, list_dir, run_shell,
                 memory_lookup]
    aliases = [read, write, patch, ls, shell, bash]
    return [FunctionTool(func=fn) for fn in canonical + aliases]


__all__ = [
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "memory_lookup",
    "read", "write", "patch", "ls", "shell", "bash",
    "adk_function_tools",
]
