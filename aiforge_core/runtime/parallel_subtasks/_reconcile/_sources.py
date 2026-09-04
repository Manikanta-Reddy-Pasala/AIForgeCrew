"""Source-file gathering, context selection, baseline snapshot, off-plan pruning.

Note: ``_SRC_EXTS`` is defined TWICE here (verbatim from the original, same order);
the SECOND definition is the effective value (last-wins) that every consumer
(``_gather_sources``/``_prune_offplan_files``/``_change_in_error``) reads at call time.

Split from ``parallel_subtasks._reconcile`` (mechanical move, behaviour identical)."""
from __future__ import annotations

import os

from .._contracts import _CONTRACT_DIR


_SRC_EXTS = (".py", ".java", ".kt", ".go", ".c", ".cc", ".cpp", ".cxx", ".h",
             ".hpp", ".js", ".mjs", ".ts", ".tsx", ".rs", ".rb", ".php", ".sh",
             ".toml", ".cfg", ".json")


def _gather_sources(cwd: str) -> list[tuple[str, str]]:
    """Every source file in the tree (ANY language), for the reconciler's
    rewrite context. Excludes deps/artifacts/venvs. Returns [(relpath, content)]."""
    out: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".aiforge-worktrees", ".aiforge-venv", ".venv", "venv",
            "__pycache__", "node_modules", "target", "build", "dist",
            ".pytest_cache", _CONTRACT_DIR)]
        for f in files:
            if f.endswith(_SRC_EXTS):
                p = os.path.join(root, f)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        out.append((os.path.relpath(p, cwd), fh.read()))
                except Exception:  # noqa: BLE001
                    pass
    return out


def _spec_goal(cwd: str) -> str:
    """The ORIGINAL GOAL from SPEC.md — re-stated at the top of the merger prompt
    to anchor the model's attention on the primary objective."""
    try:
        p = os.path.join(cwd, "SPEC.md")
        if os.path.isfile(p):
            import re as _re
            src = open(p, encoding="utf-8", errors="replace").read()
            m = _re.search(r"##\s*Goal\s*(.+?)(?:\n##\s|\Z)", src, _re.DOTALL)
            if m:
                return m.group(1).strip()[:2000]
    except Exception:  # noqa: BLE001
        pass
    return ""


_SOURCE_EXTS = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".rs",
                ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rb", ".php")


def _source_tokens(output: str) -> list[str]:
    """Every word in ``output`` that looks like a source-file path."""
    out: list[str] = []
    for raw in (output or "").replace("\\", "/").split():
        # A pytest node id ("tests/test_a.py::test_b") and a trailing quote or
        # comma both wrap the path the old pattern found INSIDE the token.
        token = raw.strip("\"'`(),;[]{}<>").split("::", 1)[0].rstrip(":,")
        # "app/store.py:12" and "src/main.rs:12:5" — compilers and tracebacks
        # append the position to the path. Drop trailing :<digits> groups.
        while ":" in token and token.rsplit(":", 1)[-1].isdigit():
            token = token.rsplit(":", 1)[0]
        stem = token.rsplit("/", 1)[-1]
        if any(stem.lower().endswith(ext) and len(stem) > len(ext)
               for ext in _SOURCE_EXTS):
            out.append(token)
    return out


def _files_in_output(cwd: str, output: str) -> set:
    """Source files REFERENCED in the failing test/build output — minimal,
    targeted context for the resolver (not the whole tree)."""
    import re as _re
    hits: set = set()
    # Tokens, not a pattern: build output is long and full of path-like runs,
    # and a quantifier over it is the denial-of-service shape a scanner asks
    # about. A word that ends in a source extension is what we are looking for.
    for m in _source_tokens(output):
        p = m.replace("\\", "/")
        if os.path.isabs(p) and p.startswith(cwd):
            p = os.path.relpath(p, cwd)
        p = p.lstrip("./")
        if os.path.isfile(os.path.join(cwd, p)):
            hits.add(p)
    return hits


def _imported_basenames(tree) -> set:
    """The last path segment of every module a parsed Python file imports —
    ``from a.b.c import x`` and ``import a.b.c`` both contribute ``c``."""
    import ast as _ast
    mods: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[-1])
        elif isinstance(node, _ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[-1])
    return mods


def _local_module_files(cwd: str, basename: str) -> set:
    """Repo-relative ``<basename>.py`` files under ``cwd`` (excluding .aiforge)."""
    fn = basename + ".py"
    out: set = set()
    for root, _d, files in os.walk(cwd):
        if fn in files:
            r = os.path.relpath(os.path.join(root, fn), cwd)
            if ".aiforge" not in r:
                out.add(r)
    return out


def _py_local_imports(cwd: str, rel: str) -> set:
    """Local module files a Python file imports (1 hop) — so the resolver sees
    both sides of a cross-file mismatch, still minimal."""
    import ast as _ast
    try:
        with open(os.path.join(cwd, rel), encoding="utf-8",
                  errors="replace") as fh:
            tree = _ast.parse(fh.read())
    except Exception:  # noqa: BLE001
        return set()
    out: set = set()
    for base in _imported_basenames(tree):
        out |= _local_module_files(cwd, base)
    return out


def _relevant_files(cwd: str, output: str) -> list:
    """Files the resolver needs: the ones named in the errors + their direct
    local imports. Falls back to the whole tree only if nothing was parsed."""
    seed = _files_in_output(cwd, output)
    if not seed:
        return _gather_sources(cwd)
    # BFS the local-import graph up to 2 hops from the failing files (test →
    # module → its deps), capped, so the resolver sees the whole failing chain
    # but never the whole tree.
    picked = set(seed)
    frontier = set(seed)
    for _ in range(2):
        nxt: set = set()
        for rel in frontier:
            if rel.endswith(".py"):
                nxt |= _py_local_imports(cwd, rel)
        nxt -= picked
        if not nxt or len(picked) >= 15:
            break
        picked |= nxt
        frontier = nxt
    out = []
    for rel in sorted(picked):
        try:
            with open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as fh:
                out.append((rel, fh.read()))
        except Exception:  # noqa: BLE001
            pass
    return out


_BASELINE_FILE = ".aiforge-baseline"


def _snapshot_baseline(cwd: str) -> int:
    """Record the source files that EXISTED before this run (an existing repo vs a
    greenfield build) so the off-plan pruner NEVER deletes pre-existing code and
    the scaffold doesn't stub over it. Returns the pre-existing source count."""
    pre = [rel for rel, _c in _gather_sources(cwd)]
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), "w", encoding="utf-8") as fh:
            fh.write("\n".join(pre))
    except Exception:  # noqa: BLE001
        pass
    return len(pre)


def _baseline_set(cwd: str) -> set:
    try:
        with open(os.path.join(cwd, _BASELINE_FILE), encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except Exception:  # noqa: BLE001
        return set()


def _is_greenfield(cwd: str) -> bool:
    """True when the workspace has (almost) no pre-existing source — a NEW project.
    Greenfield-only steps (scaffold, off-plan prune, decompose-into-full-tree) are
    DESTRUCTIVE on an existing repo, so gate them on this."""
    try:
        n = len(_baseline_set(cwd)) if os.path.exists(os.path.join(cwd, _BASELINE_FILE)) \
            else len([1 for _ in _gather_sources(cwd)])
    except Exception:  # noqa: BLE001
        n = 0
    try:
        thresh = int(os.environ.get("AIFORGE_GREENFIELD_MAX_FILES", "8"))
    except ValueError:
        thresh = 8
    return n <= thresh


def _spec_declared_paths(subs: list) -> set:
    return {str(s.get("path") or "").lstrip("/").replace("..", "")
            for s in subs if s.get("path")}


def _prune_offplan_files(cwd: str, subs: list) -> list:
    """Delete source files NOT in the SPEC's declared list — a worker or the
    reconciler sometimes invents a phantom package (e.g. tetris/game.py alongside
    the planned tetris_game.py), producing DUPLICATE modules → import/collection
    errors no runner can fix. Keeps declared files + package glue (__init__/
    conftest) + non-source (build/config). Deterministic; the tree matches the
    plan. Returns removed paths."""
    declared = _spec_declared_paths(subs)
    if not declared:
        return []
    # NEVER touch an existing repo: on a non-greenfield workspace the pruner would
    # delete the whole codebase (everything not in this task's tiny plan). Bail.
    if not _is_greenfield(cwd):
        return []
    baseline = _baseline_set(cwd)                 # pre-existing files — never delete
    removed: list = []
    for rel, _content in _gather_sources(cwd):
        r = rel.lstrip("/")
        if r in declared or r in baseline:
            continue
        base = os.path.basename(r)
        if base in ("__init__.py", "conftest.py"):
            continue                              # package glue — harmless
        if not r.endswith(_SRC_EXTS):
            continue                              # keep build/config/markup
        # a source file that is neither declared by path NOR a unique new basename
        # the plan lacks → it's off-plan pollution (often a dup of a declared file).
        try:
            os.remove(os.path.join(cwd, r))
            removed.append(r)
        except Exception:  # noqa: BLE001
            pass
    return removed


_SRC_EXTS = (".py", ".java", ".go", ".js", ".mjs", ".ts", ".tsx", ".c", ".cc",
             ".cpp", ".h", ".hpp", ".rs", ".rb", ".php", ".cs", ".kt", ".swift",
             ".scala", ".sh")


def _working_tree_changes(cwd: str) -> "set[str] | None":
    """Uncommitted (working-tree) paths from ``git status --porcelain``. None on
    git error (the tree is unusable → caller treats as "don't skip")."""
    import subprocess as _sp
    try:
        r = _sp.run(["git", "-C", cwd, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None                       # git unusable
        return {p for ln in (r.stdout or "").splitlines()
                if (p := ln[3:].strip().strip('"'))}
    except Exception:  # noqa: BLE001
        return None


def _committed_since_baseline(cwd: str) -> set[str]:
    """Paths committed since the planner's pinned 'baseline' commit — the
    parallel subtasks COMMIT their files, so ``git status`` shows a clean tree
    and misses everything they built. Best-effort: empty set on any git error."""
    import subprocess as _sp
    files: set[str] = set()
    try:
        base = _sp.run(["git", "-C", cwd, "rev-list", "--max-count=1",
                        "--grep=baseline", "HEAD"],
                       capture_output=True, text=True, timeout=10).stdout.strip()
        if base:
            d = _sp.run(["git", "-C", cwd, "diff", "--name-only", f"{base}..HEAD"],
                        capture_output=True, text=True, timeout=10)
            if d.returncode == 0:
                files.update(p.strip() for p in (d.stdout or "").splitlines()
                             if p.strip())
    except Exception:  # noqa: BLE001
        pass
    return files


def _turn_changed_source(cwd: str) -> "list[str] | None":
    """Source files THIS turn changed — BOTH uncommitted (working tree) AND
    COMMITTED since the pre-turn baseline. The parallel subtasks COMMIT their
    files, so ``git status`` shows a clean tree and misses everything they built;
    without the baseline diff a greenfield build looks like "nothing changed" and
    its compile error is wrongly ruled pre-existing. Returns None on git error
    (caller treats as "don't skip")."""
    files = _working_tree_changes(cwd)
    if files is None:
        return None
    files |= _committed_since_baseline(cwd)
    return [f for f in files if f.endswith(_SRC_EXTS)]


def _is_greenfield(cwd: str) -> bool:
    """True when this turn BUILT the project from nothing — the pre-turn baseline
    commit had NO source files. On a greenfield build there IS no 'pre-existing'
    failure: every failure is this turn's fault, so the pre-existing-skip gate
    must NOT fire (else a TRANSIENT first-test error — a cold `mvn`/`cargo`
    download whose message names no source file — is wrongly ruled environmental,
    the repair loop is skipped, and a GREEN build is verdict'd NOTCLEAN)."""
    import subprocess as _sp
    try:
        base = _sp.run(["git", "-C", cwd, "rev-list", "--max-count=1",
                        "--grep=baseline", "HEAD"],
                       capture_output=True, text=True, timeout=10).stdout.strip()
        if not base:
            return False                       # no baseline marker → can't tell
        r = _sp.run(["git", "-C", cwd, "ls-tree", "-r", "--name-only", base],
                    capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False
        return not any(f.strip().endswith(_SRC_EXTS)
                       for f in (r.stdout or "").splitlines())
    except Exception:  # noqa: BLE001
        return False


def _change_in_error(cwd: str, output: str) -> bool:
    """True if any source file THIS turn changed is named in the test/build
    error — i.e. the failure plausibly stems from the change (so repair it).
    False means the harness error references none of the changed files, so it's
    pre-existing/unrelated. Best-effort; True on any doubt (git unusable) so we
    keep the normal repair loop rather than wrongly skip a real regression."""
    import os as _os
    out = output or ""
    changed = _turn_changed_source(cwd)
    if changed is None:
        return True                           # git unusable → don't skip
    if not changed:
        return False                          # nothing source changed → not the cause
    for f in changed:
        f = f.strip().strip('"')
        if f and (f in out or _os.path.basename(f) in out
                  or _os.path.splitext(_os.path.basename(f))[0] in out):
            return True
    return False
