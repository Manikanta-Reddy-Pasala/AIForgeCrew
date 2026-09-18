"""Reporting what a run changed, and making sure new code has tests."""
from __future__ import annotations

import os

from aiforge_core.runtime.git_pr import _EXCLUDE_PATHSPECS


def _pkg():
    """``_stream``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_stream``; patch any other
    name on this module."""
    import aiforge_core.runtime.parallel_subtasks._stream as package
    return package


_SPEC_MD = "SPEC.md"

_STATUS_WORD = {"A": "added", "M": "modified", "D": "deleted",
                "R": "renamed"}


def _numstat_counts(numstat: str) -> dict:
    """``{path: (adds, dels)}`` from ``git diff --numstat``."""
    counts: dict = {}
    for ln in numstat.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            counts[parts[2]] = (parts[0], parts[1])
    return counts


def _changed_file(name_status_line: str, counts: dict, ref: list, cwd: str,
                  cap: int) -> dict | None:
    """One ``--name-status`` line as a change entry, or None to skip it."""
    parts = name_status_line.split("\t")
    if len(parts) < 2:
        return None
    status, path = parts[0][:1], parts[-1]
    if any(h in path for h in _CHANGES_HIDE):
        return None
    adds, dels = counts.get(path, ("0", "0"))
    fdiff = _pkg()._git(["diff", "--relative", *ref, "--", path], cwd).stdout or ""
    truncated = len(fdiff) > cap
    return {"path": path, "status": _STATUS_WORD.get(status, "changed"),
            "additions": _to_int(adds), "deletions": _to_int(dels),
            "diff": fdiff[:cap] + ("\n… (truncated)" if truncated else "")}


def _worktree_ref(cwd: str, start_sha: str) -> list:
    """Diff args for "the working tree now vs ``start_sha``", untracked files
    included. Compares against a tree written through a throwaway index, so the
    user's own index is untouched — `git add -N .` marked every untracked file
    "Added" in their editor's source control, and left them so."""
    try:
        from aiforge_core.runtime import checkpoints
        tree = checkpoints.worktree_tree(cwd)
    except Exception:  # noqa: BLE001
        tree = None
    if tree:
        return [start_sha, tree]
    _pkg()._git(["add", "-N", "--", ".", *_EXCLUDE_PATHSPECS], cwd)
    return [start_sha]


def _emit_changes(cwd: str, start_sha: str, include_worktree: bool = False):
    """Yield a STRUCTURED ``changes`` event — one entry per changed file with its
    status, +/- line counts, and unified diff — so the UI renders a clean PR-style
    view (file list + expandable colored diffs), not a raw blob. Used after BOTH
    the parallel pipeline (committed to base → diff ``start..HEAD``) and a
    single-agent simple run (uncommitted working tree → ``include_worktree``:
    intent-add untracked, diff ``start``)."""
    pkg = _pkg()
    if not start_sha:
        return
    try:
        cap = int(os.environ.get("AIFORGE_CHANGES_FILE_DIFF_MAX", "8000"))
    except ValueError:
        cap = 8000
    if include_worktree:
        ref = _worktree_ref(cwd, start_sha)
    else:
        ref = [f"{start_sha}..HEAD"]
    # --relative: paths relative to ``cwd`` (a project that is a subfolder of a
    # bigger repo), and only changes inside it.
    counts = _numstat_counts(pkg._git(["diff", "--relative", "--numstat", *ref], cwd).stdout or "")
    name_status = pkg._git(["diff", "--relative", "--name-status", *ref], cwd).stdout or ""
    files = [f for f in (_changed_file(ln, counts, ref, cwd, cap)
                         for ln in name_status.splitlines()) if f]
    if not files:
        return
    total_add = sum(f["additions"] for f in files)
    total_del = sum(f["deletions"] for f in files)
    yield {"type": "changes", "files": files,
           "summary": {"files": len(files), "additions": total_add,
                       "deletions": total_del}}


def _to_int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


# Generated / build / cache artifacts — never "real" source, skip in the Changes
# list across languages. Substring-matched against each changed path.
_CHANGES_HIDE = (
    # aiforge internals
    _SPEC_MD, ".aiforge-venv", ".aiforge-contracts", ".aiforge-baseline",
    ".aiforge-worktrees",
    # python
    "__pycache__", ".pyc", ".pyo", ".egg-info", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".tox/", ".coverage",
    # js / ts
    "node_modules/", "/dist/", "/.next/", "/.nuxt/", ".min.js", ".map",
    # jvm
    ".class", "/target/", "/.gradle/", "/out/",
    # go / rust / c / native
    "/vendor/", ".rlib", "/Cargo.lock", ".o", ".obj", ".a", ".so", ".dll",
    ".dylib", ".exe",
    # generic build/cache/vcs junk
    "/build/", "/bin/", "/.cache/", ".DS_Store", ".log", ".lock", ".tmp",
    ".git/",
)


_CODE_EXTS = (".py", ".go", ".js", ".ts", ".rs", ".java", ".c", ".cpp", ".rb")


def _test_path_for(path: str) -> str:
    """Conventional test path for a code file (per language). '' when the
    language's test layout is too involved to synthesise (rely on the
    architect, which is instructed to include tests)."""
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    if not stem or stem.startswith("__"):
        return ""
    if ext == ".py":
        return f"tests/test_{stem}.py"
    if ext == ".go":
        return path[:-3] + "_test.go"
    if ext in (".js", ".ts"):
        return path[:-len(ext)] + f".test{ext}"
    if ext == ".rb":
        return f"spec/{stem}_spec.rb"
    if ext == ".rs":
        return f"tests/{stem}_test.rs"
    return ""


def _ensure_test_coverage(subs: list[dict]) -> list[dict]:
    """Backstop: if the plan has NO test files, add a unit-test subtask per code
    module (so the build can be verified + self-healed). No-op when tests exist
    or the languages have no easy test convention."""
    pkg = _pkg()
    if any(pkg._is_test_subtask(s) for s in subs):
        return subs
    code = [s for s in subs if str(s.get("path") or "").endswith(_CODE_EXTS)
            and not pkg._is_test_subtask(s)]
    added: list[dict] = []
    seen = {str(s.get("path") or "") for s in subs}
    for s in code:
        tp = _test_path_for(str(s.get("path") or ""))
        if tp and tp not in seen:
            seen.add(tp)
            added.append({
                "slug": _slugify("test-" + os.path.basename(tp)), "path": tp,
                "api": [],
                "goal": f"{tp}: unit tests for {s['path']} — exercise its public "
                        f"API (from the API contract), assert real behaviour."})
    return subs + added


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import _slugify
