"""Path-exclusion + artifact-gitignore + test-path classification helpers.

Leaf group split out of the former single-file ``git_pr.py`` (grouped by
concern: excludes / git command layer / PR flow). Dependency-free so the
other submodules can layer on top without circular imports. No behaviour
change — blocks moved verbatim. Also home to the module ``log`` singleton
shared across the package.
"""
from __future__ import annotations

import logging
import os


log = logging.getLogger("aiforge.git_pr")


# Transient dirs the runner / sidecars write into the workspace. Must
# NOT land in PRs even when the target repo's own .gitignore doesn't
# cover them (most don't — graphify-out came in via the AIForgeCrew
# convention, not the target repo's). Used at status + add time so
# the diff stays scoped to real Doer work.
_EXCLUDE_PATHSPECS: tuple[str, ...] = (
    ":(exclude)graphify-out",
    ":(exclude).aiforge",
    ":(exclude).aiforge-worktrees",
    ":(exclude).aiforge-workspace",
    ":(exclude).aiforge-venv",
    ":(exclude).aiforge-contracts",
    ":(exclude).aiforge-baseline",
    ":(exclude).idea",
    ":(exclude).vscode",
    ":(exclude).DS_Store",
    ":(exclude).env",
    ":(exclude).venv",
    ":(exclude)venv",
    ":(exclude,glob)**/perf.ndjson",
    # Common build/dependency junk so "whatever is available" never
    # gets swept into a Doer commit when the target repo lacks a
    # matching .gitignore.
    ":(exclude)node_modules",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude)dist",
    ":(exclude)build",
    ":(exclude)target",
    # Python bytecode + caches — slipped into TallyConnector#10/#11
    # because those repos lacked a Python .gitignore. Belt-and-braces
    # at git add time so the target repo's own ignores are irrelevant.
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.py[cod]",
    ":(exclude,glob)**/*.class",
    ":(exclude,glob)**/*.log",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
)


# Artifact / junk directory segments + basenames + suffixes. Used by
# :func:`is_excluded_path` to filter a repo-relative path list (the pathspec
# form above is only usable as a `git add` argument, not as a plain-path
# predicate). These match as ANY path segment, so they MUST be unambiguous
# artifact names — the bare words `env`/`build`/`dist`/`target` were removed
# because they wrongly dropped legit source like `myapp/env/settings.py` or
# `pkg/build/mod.go`. Those four are excluded only at the TOP LEVEL (see
# :data:`_EXCLUDE_TOPLEVEL`), matching their top-level-only pathspecs above.
_EXCLUDE_DIR_SEGMENTS = frozenset({
    "graphify-out", ".aiforge", ".aiforge-worktrees", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", ".git",
})
# Build-output / virtualenv dirs excluded ONLY when they are the first path
# segment, to agree with the top-level `:(exclude)build|dist|target` pathspecs
# (which do NOT match nested dirs like `myapp/build/`). `env` is here as a
# TOP-LEVEL-only entry: a top-level `env/` is the common virtualenv and must be
# excluded, while a nested `myapp/env/settings.py` is legitimate source and
# still commits. (The `.env` FILE is also excluded via `_EXCLUDE_BASENAMES`.)
_EXCLUDE_TOPLEVEL = frozenset({"build", "dist", "target", "env"})
_EXCLUDE_BASENAMES = frozenset({
    ".DS_Store", ".aiforge-workspace", ".env", "perf.ndjson",
})
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".class", ".log")


def is_excluded_path(rel: str) -> bool:
    """True when ``rel`` (a repo-relative path) is an agent artifact / junk
    file that must never be staged — reconciled with :data:`_EXCLUDE_PATHSPECS`
    as a plain-path predicate: unambiguous artifact dirs match at any depth,
    while ``build``/``dist``/``target`` match only at the top level (so legit
    nested source like ``myapp/env/settings.py`` or ``svc/build/gen.go`` is
    kept)."""
    if not rel:
        return True
    norm = str(rel).strip().replace("\\", "/")
    # Strip a leading "./" prefix WITHOUT eating dotfile/dotdir leading
    # dots (lstrip("./") would turn ".aiforge" into "aiforge").
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    if not norm or norm == ".":
        return True
    segments = norm.split("/")
    if any(seg in _EXCLUDE_DIR_SEGMENTS for seg in segments):
        return True
    if segments[0] in _EXCLUDE_TOPLEVEL:
        return True
    base = segments[-1]
    if base in _EXCLUDE_BASENAMES:
        return True
    if base.endswith(_EXCLUDE_SUFFIXES):
        return True
    return False


# Agent-artifact + common build/cache lines an initialised workspace's
# .gitignore should carry, so generated files are never tracked / never pollute
# the Changes view (cleaning off across languages).
_ARTIFACT_IGNORE_LINES: tuple[str, ...] = (
    # aiforge internals
    ".aiforge/", ".aiforge-worktrees/", ".aiforge-workspace", ".aiforge-venv/",
    "graphify-out/", "perf.ndjson",
    # python
    "__pycache__/", "*.py[cod]", ".pytest_cache/", ".ruff_cache/",
    ".mypy_cache/", ".coverage", "*.egg-info/", ".venv/", "venv/",
    # node / web
    "node_modules/", "dist/", ".next/", "*.min.js.map",
    # jvm / native / go / rust
    "target/", "build/", "*.class", "*.o", "*.obj", "*.so", "*.dylib", "*.exe",
    # os junk
    ".DS_Store",
)


def ensure_artifact_gitignore(repo_root: str) -> list[str]:
    """Idempotently ensure the agent-artifact lines live in ``.gitignore``.

    Appends ONLY the missing lines (never duplicates, never clobbers the
    user's existing content). Returns the list of lines actually added
    (empty when everything was already present). Soft-fails to ``[]``.
    """
    gi = os.path.join(repo_root, ".gitignore")
    existing = ""
    if os.path.exists(gi):
        try:
            with open(gi, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            return []
    have = {ln.strip() for ln in existing.splitlines() if ln.strip()}
    missing = [ln for ln in _ARTIFACT_IGNORE_LINES if ln not in have]
    if not missing:
        return []
    chunk = ""
    if existing and not existing.endswith("\n"):
        chunk += "\n"
    chunk += "# AIForgeCrew agent artifacts\n" + "\n".join(missing) + "\n"
    try:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(chunk)
    except OSError as exc:
        log.warning("ensure_artifact_gitignore failed: %s", exc)
        return []
    return missing


# File-path heuristics for classifying a diff as test-only. Conservative:
# anything that is clearly NOT a test counts as a production change and
# unblocks the PR. False negatives (calling real prod code a "test") are
# worse than false positives — they let a fix-less PR ship.
_TEST_PATH_FRAGMENTS = (
    "/src/test/", "src/test/",          # Java/Maven layout
    "/test/", "/tests/",                # Python / Go / generic
    "/__tests__/",                      # JS/TS Jest
    "/fixtures/", "/testdata/",
    "/spec/",                           # Ruby / some JS
)
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.ts", "_test.tsx",
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    "test.java",                         # *Test.java / FooTest.java
    "_spec.rb",
)
_TEST_ALWAYS_PATHS = (
    "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
)


def _is_test_path(path: str) -> bool:
    p = path.lower()
    # Normalise so leading-segment matches work for both ``tests/foo`` and
    # ``./tests/foo``-style git outputs. Strip ONLY a leading ``./`` (loop),
    # never ``lstrip("./")`` which would also eat leading dots/slashes and turn
    # ``.tests`` into ``tests`` (mirrors is_excluded_path's documented fix).
    stripped = p
    while stripped.startswith("./"):
        stripped = stripped[2:]
    norm = "/" + stripped
    if any(p.startswith(t) or t in p for t in _TEST_ALWAYS_PATHS):
        return True
    if any(frag in norm for frag in _TEST_PATH_FRAGMENTS):
        return True
    if any(p.endswith(suf) for suf in _TEST_SUFFIXES):
        return True
    return False
