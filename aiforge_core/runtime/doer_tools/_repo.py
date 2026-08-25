"""Repo-intelligence + git tools: repo_map, impacted_tests, the codegraph_*
wrappers, git_commit + read-only git inspect (status/diff/log/blame), and
rename_symbol.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import subprocess

from ..sandbox import resolve_inside_root, root
from ._fs import record_touch

# Single source of truth for the artifact excludes lives in ``git_pr`` —
# imported here (instead of mirrored) to kill the drift the two copies
# used to suffer. ``is_excluded_path`` is the plain-path predicate for
# filtering the touched-file list.
from ..git_pr import _EXCLUDE_PATHSPECS  # noqa: E402


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
    return {"ok": True, "focus": focus, "digest": digest,
            "engine": "aider-treesitter-pagerank"}


def _digest_file_paths(digest: str) -> list[str]:
    """Extract the file paths an Aider repo-map digest lists. Each file
    section starts with a ``path/to/file.ext:`` header line."""
    import re
    paths: list[str] = []
    for line in digest.splitlines():
        m = re.match(r"^([^\s│⋮].*\.\w+):\s*$", line)
        if m:
            paths.append(m.group(1))
    return paths[:20]


# ─────────────── CodeGraph (explicit code relations, SQLite) ────────────
# The pipeline DOER builds its ADK tools from THIS module (not chat_agent),
# so the codegraph tools MUST be registered here or the Doer never receives
# them. Thin typed wrappers over runtime.tools.codegraph so ADK gets a clean
# schema (the underlying fns take an ``args`` dict). Soft-fail: return the
# tool's ``{ok: False, error}`` dict, never raise.

def codegraph_impact(symbol: str) -> dict:
    """Blast-radius of changing SYMBOL — everything that depends on it.

    Call BEFORE editing a shared symbol so you touch every file that must
    stay in sync (the cross-file linking a text grep misses). Reads a
    pre-built, auto-synced code graph (tree-sitter + SQLite), so it returns
    exact caller/impact edges, not fuzzy matches.

    Args:
      symbol: the function/method/class name to assess (e.g.
        ``publishToRemoteServer``).
    """
    from aiforge_core.runtime.tools import codegraph as _cg
    return _cg.codegraph_impact({"symbol": symbol}, cwd=str(root()))


def codegraph_callers(symbol: str) -> dict:
    """Functions/methods that CALL ``symbol`` (with file:line). Use to find
    every call site you must update when changing a signature."""
    from aiforge_core.runtime.tools import codegraph as _cg
    return _cg.codegraph_callers({"symbol": symbol}, cwd=str(root()))


def codegraph_callees(symbol: str) -> dict:
    """Functions/methods that ``symbol`` CALLS. Use to understand what a
    method depends on before editing it."""
    from aiforge_core.runtime.tools import codegraph as _cg
    return _cg.codegraph_callees({"symbol": symbol}, cwd=str(root()))


def codegraph_explore(query: str) -> dict:
    """Explore an area — relevant symbols + their source for a
    natural-language ``query`` (e.g. 'push sync priority header'). One shot
    to orient before editing."""
    from aiforge_core.runtime.tools import codegraph as _cg
    return _cg.codegraph_explore({"query": query}, cwd=str(root()))


def codegraph_query(query: str) -> dict:
    """Find symbols by name/semantics for ``query``. Returns matching
    symbols + their defining file:line."""
    from aiforge_core.runtime.tools import codegraph as _cg
    return _cg.codegraph_query({"query": query}, cwd=str(root()))



def impacted_tests(changed_files: str) -> dict:
    """Map changed files → the test files that likely cover them.

    The code-graph backend was removed (SQLite-only build), so this always
    returns an empty list and the caller runs the full test suite. Kept as a
    stable tool surface for when a code-graph backend returns.

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


# ─── Git commit ────────────────────────────────────────────────────────


def git_commit(message: str) -> dict:
    """Stage Doer-authored changes and commit with ``message``.

    The Doer runs in an ISOLATED git worktree branched from a clean base,
    so everything changed there IS the agent's work. Stages with
    ``git add -A -- . <_EXCLUDE_PATHSPECS>`` — the ``-A`` captures
    modifications, additions, DELETIONS and renames, and the artifact
    pathspecs keep the agent's own junk out. Then ``git commit -m
    <message>`` inside :func:`sandbox.root`. The staged file list is
    returned in the result. Same soft-error contract as the other Doer
    tools — failure returns ``{ok: False, error: ...}`` rather than
    raising so the agent loop survives a flaky workspace.

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

    # Isolated worktree ⇒ everything changed in it is the agent's work.
    # `git add -A` captures deletions/renames too (the old touched-list
    # dropped them); the artifact pathspecs keep agent junk out.
    add_args = ["git", "add", "-A", "--", ".", *_EXCLUDE_PATHSPECS]
    add_proc = subprocess.run(
        add_args, cwd=cwd, capture_output=True, timeout=60,
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

    # Capture the exact staged file list so the caller / UI sees what
    # was committed (and not the whole tree).
    staged_proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=cwd, capture_output=True, timeout=30,
    )
    staged = [ln for ln in staged_proc.stdout.decode(
        "utf-8", "replace").splitlines() if ln.strip()]

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
        "staged": staged,
        "staged_via": "add_all",
        "stdout": commit_proc.stdout.decode("utf-8", "replace")[:2000],
    }


# ─── Git inspect (read-only) — structured repo state without shelling out ──

def _git(argv: list, timeout: int = 30) -> dict:
    import subprocess
    try:
        r = subprocess.run(["git", *argv], cwd=str(root()),
                           capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "code": r.returncode,
                "stdout": (r.stdout or "")[-8000:],
                "stderr": (r.stderr or "")[-2000:]}
    except FileNotFoundError:
        return {"ok": False, "error": "git_not_installed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def git_status() -> dict:
    """Working-tree status (branch + staged/unstaged/untracked, porcelain).
    Read-only — the fast way to see what changed before committing."""
    return _git(["status", "--porcelain=v1", "-b"])


def git_diff(path: str = "", staged: bool = False) -> dict:
    """Show the diff. ``staged`` = the staged/cached diff; ``path`` narrows to a
    file or dir. Read-only."""
    argv = ["--no-pager", "diff"] + (["--staged"] if staged else [])
    if path:
        argv += ["--", path]
    return _git(argv)


def git_log(limit: int = 20, path: str = "") -> dict:
    """Recent commits (oneline + refs). ``limit`` capped at 200; ``path``
    narrows to a file's history. Read-only."""
    n = max(1, min(int(limit or 20), 200))
    argv = ["--no-pager", "log", f"-{n}", "--oneline", "--decorate"]
    if path:
        argv += ["--", path]
    return _git(argv)


def git_blame(path: str, start: int = 0, end: int = 0) -> dict:
    """Line-by-line last-commit attribution for a file. Optional ``start``/
    ``end`` limit to a line range. Read-only."""
    argv = ["--no-pager", "blame", "--date=short"]
    if start and end:
        argv += ["-L", f"{int(start)},{int(end)}"]
    argv += ["--", path]
    return _git(argv)


_RENAME_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
                ".c", ".cpp", ".h", ".cs", ".rb", ".php", ".kt", ".scala",
                ".swift")
_RENAME_SKIP_DIRS = (".git", "node_modules", ".venv", "venv", "dist", "build",
                     "__pycache__")


def _rename_in_one_file(fp: str, pat, new_name: str, dry_run: bool) -> int:
    """Count (and, unless dry_run, apply) the rename in one file. Returns the
    number of occurrences, or 0 when unreadable / unmatched."""
    import os as _os
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except Exception:  # noqa: BLE001
        return 0
    c = len(pat.findall(txt))
    if not c or dry_run:
        return c
    try:
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(pat.sub(new_name, txt))
        record_touch(fp)
    except Exception:  # noqa: BLE001
        return 0
    return c


def _iter_rename_targets(root_p: str):
    """Yield every code file under ``root_p``, vendor/build dirs pruned."""
    import os as _os
    for dirpath, dirnames, filenames in _os.walk(root_p):
        dirnames[:] = [d for d in dirnames if d not in _RENAME_SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(_RENAME_EXTS):
                yield _os.path.join(dirpath, fn)


def rename_symbol(name: str, new_name: str, path: str = ".",
                  dry_run: bool = True) -> dict:
    """Whole-word rename of an identifier across code files under ``path``.
    TEXT-based (word-boundary) — precise for unique identifiers, but review the
    diff for false hits in strings/comments. ``dry_run`` (default) only reports
    what WOULD change; pass dry_run=false to apply. Not an LSP semantic rename."""
    import os as _os
    import re
    if not name or not new_name:
        return {"ok": False, "error": "need 'name' and 'new_name'"}
    root_p = str(resolve_inside_root(path))
    pat = re.compile(r"\b" + re.escape(name) + r"\b")
    hits: list[dict] = []
    changed = 0
    for fp in _iter_rename_targets(root_p):
        c = _rename_in_one_file(fp, pat, new_name, dry_run)
        if not c:
            continue
        hits.append({"file": _os.path.relpath(fp, str(root())),
                     "occurrences": c})
        if not dry_run:
            changed += c
    total = sum(h["occurrences"] for h in hits)
    return {"ok": True, "name": name, "new_name": new_name,
            "dry_run": dry_run, "files": hits, "total_occurrences": total,
            "applied": (0 if dry_run else changed),
            "note": ("preview only — pass dry_run=false to apply"
                     if dry_run else "applied; review the diff")}
