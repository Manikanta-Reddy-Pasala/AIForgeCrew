"""Running a bare Python project's tests: finding test files and third-party
imports, a pytest venv, and capturing the run."""
from __future__ import annotations

import os

_VENV = '.venv'
_WORKTREES = '.aiforge-worktrees'
_AIFORGE_VENV = '.aiforge-venv'


def _pkg():
    """``integration_report``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``integration_report``; patch any other
    name on this module."""
    import aiforge_core.runtime.integration_report as package
    return package


# stdlib top-level module names we must NOT try to pip-install.
_STDLIB = frozenset((
    "os", "sys", "re", "json", "math", "random", "time", "typing", "abc",
    "collections", "dataclasses", "enum", "functools", "itertools", "pathlib",
    "subprocess", "threading", "queue", "logging", "unittest", "argparse",
    "copy", "io", "struct", "types", "contextlib", "datetime", "string",
    "textwrap", "operator", "heapq", "bisect", "hashlib", "uuid", "shutil",
    "tempfile", "glob", "traceback", "warnings", "inspect", "importlib",
    "asyncio", "socket", "select", "signal", "pytest", "__future__", "test",
    "tests",
))


def _stdlib_names() -> frozenset:
    """Authoritative stdlib module set. Python 3.10+ exposes the real list via
    ``sys.stdlib_module_names`` — use it so no stdlib name (secrets, hashlib,
    sqlite3, …) is ever mis-flagged as a pip-installable third-party dep (a
    single bad name breaks the whole venv install → pytest missing → the test
    gate goes blind). Fall back to the hand-list on older interpreters."""
    import sys as _sys
    names = getattr(_sys, "stdlib_module_names", None)
    if names:
        return frozenset(names) | _STDLIB
    return _STDLIB


_IMPORT_SCAN_SKIP = (".git", _VENV, _AIFORGE_VENV, "node_modules",
                     "__pycache__", _WORKTREES)


def _imported_modules(cwd: str, pat, std: set) -> set[str]:
    """Every non-stdlib top-level module name imported in any .py under ``cwd``."""
    mods: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _IMPORT_SCAN_SKIP]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, f), encoding="utf-8",
                          errors="replace") as fh:
                    src = fh.read()
            except Exception:  # noqa: BLE001
                continue
            mods.update(m for m in pat.findall(src) if m and m not in std)
    return mods


def _local_names(cwd: str) -> set[str]:
    """Top-level names that are LOCAL to the tree (a dir or a .py stem in
    ``cwd``) — not third-party, so not something to pip-install."""
    if not os.path.isdir(cwd):
        return set()
    entries = os.listdir(cwd)
    return set(entries) | {os.path.splitext(f)[0] for f in entries}


def _third_party_imports(cwd: str) -> list[str]:
    """Top-level third-party modules imported anywhere in the tree — so a bare
    (marker-less) project's test venv can pip-install them (pygame, numpy, …)."""
    import re as _re
    pat = _re.compile(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", _re.MULTILINE)
    mods = _imported_modules(cwd, pat, _stdlib_names())
    local = _local_names(cwd)
    return sorted(m for m in mods if m not in local)


_TEST_SKIP_DIRS = frozenset((
    _AIFORGE_VENV, _WORKTREES, _VENV, "venv", "env",
    "__pycache__", ".git", "node_modules", "site-packages", ".tox",
    ".pytest_cache", "build", "dist",
))


def _python_test_files(cwd: str) -> list[str]:
    """Test files under ``cwd``, skipping vendored/artifact dirs. Filters on path
    components RELATIVE to cwd — a substring check on the absolute path wrongly
    drops everything when the workspace itself lives under e.g.
    ~/.aiforge/chat-workspaces (the '.aiforge' segment is in the ROOT, not an
    artifact inside the tree), which silently disabled test discovery — and thus
    the reconcile's pass/fail gate — for every chat-mode run."""
    import glob
    hits = glob.glob(os.path.join(cwd, "**", "test_*.py"), recursive=True)
    hits += glob.glob(os.path.join(cwd, "**", "*_test.py"), recursive=True)
    out = []
    for h in hits:
        rel = os.path.relpath(h, cwd)
        parts = rel.split(os.sep)
        if any(p in _TEST_SKIP_DIRS for p in parts):
            continue
        out.append(h)
    return out


def _ensure_pytest_venv(cwd: str, venv: str, py: str, timeout: int) -> str:
    """Make ``py`` a venv with pytest + the tree's deps importable, creating and
    installing as needed. A PRIOR round may have created the venv but failed to
    install pytest (a single bad dep name aborts the whole ``pip install``),
    leaving a venv with no pytest and the gate permanently blind — so this checks
    the pytest IMPORT, not just venv existence."""
    import subprocess
    import sys

    def _has_pytest() -> bool:
        if not os.path.exists(py):
            return False
        c = subprocess.run([py, "-c", "import pytest"],
                           capture_output=True, timeout=60)
        return c.returncode == 0

    if _has_pytest():
        return ""
    if not os.path.exists(py):
        subprocess.run([sys.executable, "-m", "venv", venv],
                       capture_output=True, timeout=120)
    # CORE test deps FIRST, in their own call that MUST succeed — the pytest
    # plugins models reference via pyproject addopts (cov, asyncio, mock) so a
    # `--cov`/`@pytest.mark.asyncio` config doesn't exit with "unrecognized
    # arguments" and zero signal.
    core = subprocess.run([py, "-m", "pip", "-q", "install", "--no-input",
                           "pytest", "pytest-cov", "pytest-asyncio", "pytest-mock",
                           "pytest-timeout", "ruff"],
                          capture_output=True, text=True, timeout=timeout)
    # Third-party imports BEST-EFFORT and one at a time — a single
    # unresolvable/mis-detected name (a stray stdlib module, a private package)
    # must NOT abort the whole install and strand pytest. Each failure is
    # isolated; a genuinely-missing import just surfaces as a real test error.
    for dep in _pkg()._third_party_imports(cwd):
        subprocess.run([py, "-m", "pip", "-q", "install", dep],
                       capture_output=True, timeout=timeout)
    req = os.path.join(cwd, "requirements.txt")
    if os.path.exists(req):
        subprocess.run([py, "-m", "pip", "-q", "install", "-r", req],
                       capture_output=True, timeout=timeout)
    if _has_pytest():
        return ""
    # Say WHY instead of a bare "No module named pytest" that reads like a
    # defect in the generated code (it is an index / network / credential
    # problem — pip goes to PIP_INDEX_URL, which run.sh points at the
    # internal index).
    tail = ((core.stderr or "") + (core.stdout or "")).strip()[-800:]
    return ("could not install pytest into the build's .aiforge-venv (index: "
            + (os.environ.get("PIP_INDEX_URL") or "pip default") + ")"
            + (":\n" + tail if tail else ""))


def _pytest_timeout_args() -> list[str]:
    """Per-TEST timeout args (pytest-timeout). A generated ``while True`` worker
    otherwise hangs the WHOLE run until the subprocess timeout, masking every
    result; a per-test cap turns it into ONE visible "Timeout" the reconcile can
    target. SIGALRM method (the default) reliably INTERRUPTS a CPU-bound loop —
    pytest runs as its own subprocess on the main thread here."""
    try:
        ptt = max(3, int(os.environ.get("AIFORGE_PYTEST_TIMEOUT", "20")))
    except ValueError:
        ptt = 20
    return ["--timeout", str(ptt)]


def _run_pytest_capturing(py: str, cwd: str, env: dict, timeout: int):
    """Run pytest, retrying once with addopts stripped when a broken CONFIG (a
    plugin/addopts the tree can't satisfy → usage error, no tests collected)
    stopped it from starting. Returns ``(returncode, combined_output)``."""
    import subprocess
    to = _pytest_timeout_args()
    p = subprocess.run([py, "-m", "pytest", "-q", *to], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=timeout)
    out = p.stdout + p.stderr
    config_broke = (p.returncode == 4
                    or "unrecognized arguments" in out
                    or "usage: pytest" in out.lower())
    if config_broke:
        p = subprocess.run(
            [py, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", *to], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
    return p.returncode, out


def run_bare_python_tests(cwd: str, timeout: int = 300):
    """Run pytest on a bare (marker-less) Python tree via a managed venv that
    pip-installs pytest + the tree's third-party imports. Returns ``(ok, output)``
    or ``None`` when there are no tests (nothing to check). The venv lives at
    ``.aiforge-venv`` (git-ignored) and is reused across reconcile rounds."""
    pkg = _pkg()
    if not pkg._python_test_files(cwd):
        return None
    venv = os.path.join(cwd, pkg._AIFORGE_VENV)
    py = os.path.join(venv, "bin", "python")
    try:
        why = pkg._ensure_pytest_venv(cwd, venv, py, timeout)
        if why:
            return False, why
        env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        rc, out = pkg._run_pytest_capturing(py, cwd, env, timeout)
        ok = rc == 0
        # LINT gate (Python leg): when tests otherwise PASS, run the real-bug ruff
        # codes via the managed venv's ruff. The generic multi-language dispatch
        # in ``integration_report`` handles other stacks.
        if ok and os.environ.get("AIFORGE_LINT_GATE", "1") not in ("0", "false"):
            lok, lout = pkg._static_lint_python(cwd, py, env)
            if not lok:
                ok = False
                out += lout
        return ok, out[-4000:]
    except Exception:  # noqa: BLE001
        return None
