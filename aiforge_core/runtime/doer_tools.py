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

# Pathspecs to keep transient cache dirs out of Doer-created commits.
# Mirrors ``runtime.git_pr._EXCLUDE_PATHSPECS`` so manual commits
# behave like the auto-PR step at end-of-ticket.
_EXCLUDE_PATHSPECS: tuple[str, ...] = (
    ":(exclude)graphify-out",
    ":(exclude).aiforge",
    ":(exclude).aiforge-worktrees",
    ":(exclude).idea",
    ":(exclude).vscode",
    ":(exclude).DS_Store",
)


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


def repo_map(focus: str = "", token_budget: int = 1024) -> dict:
    """Ranked structural map of the repo — pure tree-sitter AST, no vectors.

    Runs Aider's RepoMap (tree-sitter tags + PageRank) over the worktree
    and returns a token-budgeted digest of the most relevant files +
    their key symbols/signatures. This is the grep+AST navigation path:
    exact, zero-staleness (reads HEAD), no embedding / re-index cost.

    Prefer this for "what's in this repo / where do the important
    symbols live" over fuzzy vector recall. Combine with ``grep_repo``
    (exact pattern) and ``graphify_lookup`` (typed call/use edges) to
    pin down specifics.

    Args:
      focus: natural-language hint (the ticket goal / a symbol name).
        Used as PageRank personalisation so the map centres on what you
        asked about. Empty = generic top-K digest.
      token_budget: rough token cap on the digest (default 1024).
    """
    try:
        from aiforge_core.memory.code_context import aider_digest
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"repo_map unavailable: {exc}"}
    digest = aider_digest(
        str(root()), chat_files=[], token_budget=token_budget,
        user_text=focus or "",
    )
    if not digest:
        return {"ok": False, "error": "empty map (repo too small or "
                "aider/tree-sitter unavailable)", "digest": ""}
    # Enrich with Graphify INFERRED call/use edges that tree-sitter alone
    # misses — the file paths the aider digest surfaced are the seed.
    # Soft: empty string when Neo4j/Graphify is unavailable.
    try:
        from aiforge_core.memory.code_context import graph_neighbours
        files = _digest_file_paths(digest)
        neighbours = graph_neighbours(files) if files else ""
    except Exception:  # noqa: BLE001
        neighbours = ""
    if neighbours:
        digest = f"{digest}\n\n{neighbours}"
    return {"ok": True, "focus": focus, "digest": digest,
            "engine": "aider-treesitter-pagerank+graphify"}


def _digest_file_paths(digest: str) -> list[str]:
    """Extract the file paths an Aider repo-map digest lists. Each file
    section starts with a ``path/to/file.ext:`` header line."""
    import re
    paths: list[str] = []
    for line in digest.splitlines():
        m = re.match(r"^([^\s│⋮].*\.[A-Za-z0-9_]+):\s*$", line)
        if m:
            paths.append(m.group(1))
    return paths[:20]


def impacted_tests(changed_files: str) -> dict:
    """Map changed files → the test files that likely cover them.

    Walks the Neo4j File_v2/Symbol_v2 graph (MENTIONS/CALLS/IMPORTS) back
    up to test files. Pass the result's ``pattern`` to ``run_tests`` /
    ``pytest -k`` / ``mvn -Dtest=`` to run only the relevant slice
    instead of the full suite. Soft-fails to an empty list (→ run all)
    when the graph is unavailable.

    Args:
      changed_files: comma- or space-separated repo-relative paths you
        edited (e.g. ``"src/main/java/Foo.java, src/main/java/Bar.java"``).
    """
    import os as _os
    raw = (changed_files or "").replace(",", " ").split()
    if not raw:
        return {"ok": False, "error": "no changed files given", "tests": []}
    repo = _os.environ.get("AIFORGE_AFM_REPO", "").strip()
    if not repo:
        return {"ok": False, "error": "AIFORGE_AFM_REPO unset", "tests": []}
    try:
        from aiforge_core.runtime.diff_impact import impacted_tests as _impacted
        tests = _impacted(repo, raw)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "tests": []}
    return {"ok": True, "tests": tests, "pattern": ",".join(tests),
            "count": len(tests)}


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
        # Public/arbitrary web fetch — keep stdlib default TLS verification
        # regardless of AIFORGE_LLM_SSL_VERIFY (that toggle is scoped to
        # AIForge's own self-hosted endpoints, see aiforge_core.net.ssl).
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


# ─── Git commit ────────────────────────────────────────────────────────


def git_commit(message: str) -> dict:
    """Stage Doer-authored changes and commit with ``message``.

    Runs ``git add -A -- . <_EXCLUDE_PATHSPECS>`` then
    ``git commit -m <message>`` inside :func:`sandbox.root`. Same
    soft-error contract as the other Doer tools — failure returns
    ``{ok: False, error: ...}`` rather than raising so the agent loop
    survives a flaky workspace.

    Skip-empty: if nothing is staged after ``git add`` (i.e.
    ``git diff --cached --quiet`` exits 0), returns
    ``{ok: True, skipped: "nothing to commit"}`` without invoking
    ``git commit``. Lets the Doer call ``git_commit`` after every
    milestone without worrying about empty-tree errors.

    Used for in-task milestone snapshots (models written, schemas
    written, etc.) so progress is captured even if a later step
    blocks. The end-of-ticket PR step in :mod:`runtime.git_pr` still
    runs after the Doer finishes — these milestone commits flow into
    the same branch.
    """
    if not message or not str(message).strip():
        return {"ok": False, "error": "empty commit message"}
    cwd = str(root())

    add_proc = subprocess.run(
        ["git", "add", "-A", "--", ".", *_EXCLUDE_PATHSPECS],
        cwd=cwd, capture_output=True, timeout=60,
    )
    if add_proc.returncode != 0:
        return {
            "ok": False,
            "error": "git_add_failed",
            "stderr": add_proc.stderr.decode("utf-8", "replace")[:2000],
        }

    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=cwd, capture_output=True, timeout=30,
    )
    # `git diff --cached --quiet` exits 0 when nothing is staged, 1
    # when there ARE staged changes. Anything else = git error.
    if diff_proc.returncode == 0:
        return {"ok": True, "skipped": "nothing to commit"}
    if diff_proc.returncode not in (0, 1):
        return {
            "ok": False,
            "error": "git_diff_failed",
            "stderr": diff_proc.stderr.decode("utf-8", "replace")[:2000],
        }

    commit_proc = subprocess.run(
        ["git", "commit", "-m", str(message)],
        cwd=cwd, capture_output=True, timeout=60,
    )
    if commit_proc.returncode != 0:
        return {
            "ok": False,
            "error": "git_commit_failed",
            "stderr": commit_proc.stderr.decode("utf-8", "replace")[:2000],
            "stdout": commit_proc.stdout.decode("utf-8", "replace")[:2000],
        }
    return {
        "ok": True,
        "message": str(message),
        "stdout": commit_proc.stdout.decode("utf-8", "replace")[:2000],
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


def edit(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch`.

    Local models (Qwen3-Coder) consistently emit a tool call named
    ``edit`` — the Claude/aider str-replace convention — which wasn't
    registered. ONE-7 spent 37 minutes looping "Tool 'edit' not found",
    never wrote a file, and blocked with ``no_changes``. Registering
    the alias closes that gap so the local-model Doer can actually
    mutate files."""
    return file_patch(path, old_text, new_text)


def str_replace(path: str, old_text: str, new_text: str) -> dict:
    """Alias for :func:`file_patch` (OpenHands str_replace_editor name)."""
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


def commit(message: str) -> dict:
    """Alias for :func:`git_commit`."""
    return git_commit(message)


def git_add_commit(message: str) -> dict:
    """Alias for :func:`git_commit`."""
    return git_commit(message)


# Claude-Code / OpenHands meta-tools local models (Qwen3-Coder) emit
# from training but which have no side effect in our pipeline. ADK
# hard-errors on an unregistered function name and that BLOCKS the whole
# ticket (ONE-7: 'Tool todo_write not found' → no_changes → blocked),
# so we register them as no-ops that return success and nudge the model
# back toward the real tools. Cheaper than letting one stray call kill a
# 30-minute run.

def todo_write(todos: str = "", **_kw) -> dict:
    """No-op planning scratchpad (Claude-Code TodoWrite). Accepted so a
    stray call doesn't abort the run; the plan already lives in state."""
    return {"ok": True, "note": "todo noted (no-op); use editor/bash to act"}


def todowrite(todos: str = "", **_kw) -> dict:
    """Alias spelling for :func:`todo_write`."""
    return todo_write(todos)


def glob(pattern: str = "*", path: str = ".") -> dict:
    """Claude-Code Glob → delegate to grep_repo's file search. Falls
    back to list_dir when no pattern."""
    return grep_repo(pattern, path)


def task(description: str = "", **_kw) -> dict:
    """No-op for Claude-Code Task/Agent spawns — the Doer already runs
    inside the pipeline; sub-agent spawning goes through delegate_to_agent,
    not this name. Accepted so a stray call doesn't abort the run."""
    return {"ok": True, "note": "task no-op; use editor/bash directly"}


# ─── ADK wiring ────────────────────────────────────────────────────────


def adk_function_tools() -> list:
    """Return the Doer's tool list as ADK ``FunctionTool`` instances.

    Lazy import keeps unit tests ADK-free.

    Order — OpenHands-parity tools first (editor/bash/think/finish from
    :mod:`aiforge_core.runtime.tools`), then legacy canonical names, then
    aliases. NOTE: every agent built with this factory currently sees the
    FULL set — the ``agents.yaml`` allowed/forbidden lists are enforced
    by prompt contract + the GA/harness layers, not filtered here.

    Legacy tools (file_read/file_write/file_patch/list_dir/run_shell)
    are DEPRECATED — kept one release as escape hatches for hallucinated
    names. Doer's ``forbidden`` list in ``agents.yaml`` blocks them.
    """
    from google.adk.tools import FunctionTool

    # New OH-parity surface (sub-project #1)
    from aiforge_core.runtime.tools.bash import bash as new_bash
    from aiforge_core.runtime.tools.cognition import finish, think
    from aiforge_core.runtime.tools.editor import editor
    from aiforge_core.runtime.tools.ensure_runtime import ensure_runtime
    from aiforge_core.runtime.tools.project_runner import project

    new_canonical = [editor, new_bash, think, finish, ensure_runtime, project]
    legacy_canonical = [file_read, file_write, file_patch, list_dir, run_shell,
                        grep_repo, repo_map, impacted_tests, fetch_url,
                        git_commit, memory_lookup, graphify_lookup]
    aliases = [read, write, patch, edit, str_replace, ls, shell,
               grep, search, http_get, web_fetch,
               commit, git_add_commit,
               todo_write, todowrite, glob, task]
    return [FunctionTool(func=fn) for fn in new_canonical + legacy_canonical + aliases]


__all__ = [
    "file_read", "file_write", "file_patch", "list_dir", "run_shell",
    "grep_repo", "repo_map", "impacted_tests", "fetch_url", "git_commit",
    "memory_lookup", "graphify_lookup",
    "read", "write", "patch", "edit", "str_replace", "ls", "shell", "bash",
    "grep", "search", "http_get", "web_fetch",
    "commit", "git_add_commit",
    "todo_write", "todowrite", "glob", "task",
    "adk_function_tools",
]
