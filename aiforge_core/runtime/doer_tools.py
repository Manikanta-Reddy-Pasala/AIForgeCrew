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
import shutil
import subprocess
import urllib.error
import urllib.request

from .graphify_lookup_tool import graphify_lookup
from .memory_lookup_tool import memory_lookup
from .sandbox import resolve_inside_root, root
from .syntax_guard import validate_syntax


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


# ─── Repo grep ─────────────────────────────────────────────────────────


_GREP_DEFAULT_EXCLUDES = (
    ".git", "node_modules", "target", "build", "dist", ".venv", "venv",
    "__pycache__", ".mvn", ".idea", ".gradle",
)


def grep_repo(pattern: str, path: str = ".") -> dict:
    """Recursive regex search over the repo. Returns matching ``{file,
    line, text}`` rows.

    Uses ripgrep when available (10-100x faster on large trees), falls
    back to ``grep -RnE``. Both produce the same shape so the model
    can't tell the difference. Output capped at 200 hits / 8 KB to
    keep the agent context small.

    Args:
      pattern: extended regex (anchors, groups, alternation OK).
      path: search root, repo-relative; default = whole repo.
    """
    if not pattern or not pattern.strip():
        return {"ok": False, "error": "empty pattern"}
    try:
        target = resolve_inside_root(path) if path and path != "." else root()
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists():
        return {"ok": False, "error": f"not found: {path}"}

    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "--with-filename", "--line-number",
               "--max-count", "200", "--max-filesize", "1M",
               "-e", pattern, str(target)]
        for ex in _GREP_DEFAULT_EXCLUDES:
            cmd[1:1] = ["--glob", f"!{ex}"]
    else:
        excludes = []
        for ex in _GREP_DEFAULT_EXCLUDES:
            excludes += [f"--exclude-dir={ex}"]
        cmd = ["grep", "-RnE", *excludes, "--", pattern, str(target)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, cwd=root(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"binary missing: {exc}"}

    out = proc.stdout.decode("utf-8", "replace")
    hits: list[dict] = []
    repo_root = str(root())
    for line in out.splitlines()[:200]:
        # rg/grep both emit `path:lineno:text`. Split only twice so a
        # colon in code lands in `text`, not the path/lineno fields.
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_abs, lineno, text = parts
        rel = file_abs[len(repo_root):].lstrip("/") if file_abs.startswith(repo_root) else file_abs
        hits.append({"file": rel, "line": int(lineno) if lineno.isdigit() else 0,
                     "text": text[:240]})
    return {
        "ok": True,
        "pattern": pattern,
        "path": path or ".",
        "engine": "rg" if rg else "grep",
        "hits": hits,
        "truncated": len(out.splitlines()) > 200,
    }


# ─── HTTP fetch ────────────────────────────────────────────────────────


_FETCH_MAX_BYTES = 256 * 1024
_FETCH_TIMEOUT_S = 15


def fetch_url(url: str) -> dict:
    """GET an http(s) URL and return the body as text.

    Used for fetching public docs / spec pages mid-task. NOT a browser:
    no cookies, no JS, no redirects to file://. Body capped at 256 KB,
    timeout 15s, http(s) only.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)"}
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "AIForgeCrew-Doer/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            raw = resp.read(_FETCH_MAX_BYTES + 1)
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http {exc.code}", "status": exc.code}
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"url error: {exc.reason}"}
    except (TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)}

    truncated = len(raw) > _FETCH_MAX_BYTES
    body = raw[:_FETCH_MAX_BYTES].decode("utf-8", "replace")
    return {
        "ok": True,
        "url": url,
        "status": status,
        "content_type": ctype,
        "body": body,
        "bytes": len(raw),
        "truncated": truncated,
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


def grep(pattern: str, path: str = ".") -> dict:
    """Alias for :func:`grep_repo`."""
    return grep_repo(pattern, path)


def search(pattern: str, path: str = ".") -> dict:
    """Alias for :func:`grep_repo`."""
    return grep_repo(pattern, path)


def http_get(url: str) -> dict:
    """Alias for :func:`fetch_url`."""
    return fetch_url(url)


def web_fetch(url: str) -> dict:
    """Alias for :func:`fetch_url`."""
    return fetch_url(url)


# ─── ADK wiring ────────────────────────────────────────────────────────


def adk_function_tools() -> list:
    """Return the Doer's tool list as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.

    Order — canonical names first so they show up at the top of the
    schema dump the model consumes; aliases follow as escape hatches.
    """
    from google.adk.tools import FunctionTool
    canonical = [file_read, file_write, file_patch, list_dir, run_shell,
                 grep_repo, fetch_url,
                 memory_lookup, graphify_lookup]
    aliases = [read, write, patch, ls, shell, bash,
               grep, search, http_get, web_fetch]
    return [FunctionTool(func=fn) for fn in canonical + aliases]


__all__ = [
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "grep_repo", "fetch_url",
    "memory_lookup", "graphify_lookup",
    "read", "write", "patch", "ls", "shell", "bash",
    "grep", "search", "http_get", "web_fetch",
    "adk_function_tools",
]
