"""Filesystem + shell tools the Doer LlmAgent calls during the v6 ADK
pipeline. Wired into ``runtime.adk_runner`` as ``google.adk.tools.FunctionTool``.

Scope rules (basic — full ScopeGuard wired in a follow-up):

* Every path argument is resolved against ``AIFORGE_REPO_ROOT`` (default
  ``$HOME/aiforge_workspace``). Any resolved path that escapes the root
  via ``..`` raises ``PermissionError`` so a model hallucination never
  scribbles outside the workspace.
* ``run_shell`` runs in the same root with a 90-second hard timeout.
* No network, no privilege escalation. The tools are intentionally thin
  primitives — higher-level skills compose them.

Each tool returns a JSON-serialisable dict so ADK can persist tool
results in the session state. Failures return ``{"ok": False,
"error": "<msg>"}`` instead of raising — keeps the agent loop alive
while still surfacing the problem to the model.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _root() -> Path:
    raw = os.environ.get(
        "AIFORGE_REPO_ROOT",
        str(Path.home() / "aiforge_workspace"),
    )
    p = Path(raw).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_inside_root(rel: str) -> Path:
    """Resolve ``rel`` against the repo root and reject path-traversal."""
    root = _root()
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise PermissionError(
            f"path {rel!r} resolves outside AIFORGE_REPO_ROOT={root}"
        )
    return target


def file_read(path: str) -> dict:
    """Read a UTF-8 text file relative to the repo root.

    Args:
      path: relative path (e.g. ``docs/README.md``).

    Returns:
      ``{ok, path, content, bytes}`` on success, or ``{ok: False, error}``.
    """
    try:
        p = _resolve_inside_root(path)
        if not p.is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": path,
                "content": text, "bytes": len(text.encode("utf-8"))}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def _validate_syntax(path: str, content: str) -> tuple[bool, str]:
    """Cheap syntax sniff so the Doer can self-correct before commit.

    Designed to catch the model's most common hallucinations:

    * unbalanced ``{}``/``()``/``[]`` — half-truncated output
    * Python: tries ``compile()`` with a friendly error
    * Java/Kotlin: refuses Python-style ``foo = bar`` kwargs in calls

    Returns ``(ok, error_msg)``. Empty string on the happy path.
    """
    if not content or not content.strip():
        return False, "empty file content"
    pairs = {"{": "}", "(": ")", "[": "]"}
    for opener, closer in pairs.items():
        if content.count(opener) != content.count(closer):
            return False, (
                f"unbalanced {opener}{closer} "
                f"({content.count(opener)} vs {content.count(closer)})"
            )
    if path.endswith(".py"):
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            return False, f"python syntax: {exc.msg} at line {exc.lineno}"
    if path.endswith((".java", ".kt")):
        import re as _re
        # `foo = bar,` inside a method call — Python kwargs in Java.
        # Allow it inside annotations like `@Bean(name = "x")`.
        if _re.search(r"\b\w+\s*=\s*\w+[\s,]", content) and "(" in content:
            if not _re.search(r"@\w+\s*\(", content):
                return False, "java/kotlin: looks like Python-style kwargs in call"
    return True, ""


def file_write(path: str, content: str) -> dict:
    """Create or overwrite a UTF-8 text file relative to the repo root.

    Parent directories are created as needed. Existing files are replaced.
    Runs a syntax sanity check first — when the check fails the file is
    NOT written and the error string is surfaced so the Doer can fix
    its draft on the next turn instead of leaking corrupt output to
    disk. Set ``AIFORGE_DOER_SKIP_SYNTAX=1`` to bypass (debug only).

    Args:
      path: relative path under the repo root.
      content: full UTF-8 text to write.

    Returns:
      ``{ok, path, bytes}`` on success, or ``{ok: False, error}``.
    """
    try:
        p = _resolve_inside_root(path)
        if os.environ.get("AIFORGE_DOER_SKIP_SYNTAX", "0") not in ("1", "true"):
            ok, err = _validate_syntax(path, content)
            if not ok:
                return {"ok": False, "error": f"syntax_invalid: {err}",
                        "hint": "fix the syntax and call file_write again; "
                                "or call memory_lookup if you need symbol info"}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path,
                "bytes": len(content.encode("utf-8"))}
    except (PermissionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def file_patch(path: str, old_text: str, new_text: str) -> dict:
    """Replace the FIRST occurrence of ``old_text`` with ``new_text``.

    Use this for surgical edits when a full rewrite is overkill.
    ``old_text`` must match an exact substring of the file (whitespace
    sensitive). Failure modes:

    * file not found  → ``error: not_found``
    * old_text absent → ``error: old_text_not_found``
    * old_text appears more than once → ``error: ambiguous_match``
      (caller must pass more surrounding context to disambiguate).

    Args:
      path: relative path under the repo root.
      old_text: exact substring to replace.
      new_text: replacement substring.

    Returns:
      ``{ok, path, replaced: True}`` on success, or ``{ok: False, error}``.
    """
    try:
        p = _resolve_inside_root(path)
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
    """List the contents of a directory under the repo root.

    Args:
      path: relative directory; ``""`` (default) lists the root.

    Returns:
      ``{ok, path, entries: [{name, kind: file|dir|other}]}``.
    """
    try:
        p = _resolve_inside_root(path) if path else _root()
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

    The command is invoked through ``/bin/sh -c`` so pipes and redirects
    work. Hard timeout 90 seconds; output is truncated to 8000 bytes per
    stream so a runaway test suite cannot blow up the session state.

    Args:
      cmd: the shell command line to execute.

    Returns:
      ``{ok, returncode, stdout, stderr, truncated}``. ``ok`` is True
      when ``returncode == 0``. The model should inspect ``stderr`` /
      ``returncode`` rather than trusting ``ok`` for nuanced calls.
    """
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=_root(),
            capture_output=True, timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "timeout",
                "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:8000],
                "stderr": (exc.stderr or b"").decode("utf-8", "replace")[:8000]}
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    truncated = len(out) > 8000 or len(err) > 8000
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": out[:8000], "stderr": err[:8000],
        "truncated": truncated,
    }


def memory_lookup(query: str, k: int = 6) -> dict:
    """Active mid-task retrieval over AiForgeMemory.

    The Doer calls this when it hits an unknown class/method/concept it
    can't find by reading nearby files. Returns the same hybrid hits the
    pipeline pre-fetches at ticket-claim time: vector + fulltext +
    1-hop graph, fused via RRF.

    Args:
      query: free-form text — symbol, error message, or natural question.
      k: max hits to return (default 6, capped at 12).

    Returns:
      ``{ok, hits: [{source, score, body}], used_sources: [...]}`` or
      ``{ok: False, error}`` when the memory backend is unreachable.
    """
    try:
        from aiforge_core.memory import unified_query as _uq
    except Exception as exc:
        return {"ok": False, "error": f"memory backend missing: {exc}"}
    try:
        result = _uq.query(query, limit=max(1, min(12, int(k or 6))))
    except Exception as exc:
        return {"ok": False, "error": f"memory query failed: {exc}"}
    raw_hits = result.get("hits") or []
    hits: list[dict] = []
    for h in raw_hits[:k]:
        body = (h.get("text") or h.get("body") or h.get("summary") or "")[:600]
        hits.append({
            "source": h.get("source", "?"),
            "score": float(h.get("score", 0.0) or 0.0),
            "body": body,
        })
    return {"ok": True, "hits": hits,
            "used_sources": result.get("used_sources") or []}


# ─── ADK wiring helper ────────────────────────────────────────────────


def adk_function_tools() -> list:
    """Return the Doer's tool list as ``google.adk.tools.FunctionTool``
    instances. Lazy import keeps unit tests ADK-free."""
    from google.adk.tools import FunctionTool
    return [
        FunctionTool(func=file_read),
        FunctionTool(func=file_write),
        FunctionTool(func=file_patch),
        FunctionTool(func=list_dir),
        FunctionTool(func=run_shell),
        FunctionTool(func=memory_lookup),
    ]


__all__ = [
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "memory_lookup", "adk_function_tools",
]
