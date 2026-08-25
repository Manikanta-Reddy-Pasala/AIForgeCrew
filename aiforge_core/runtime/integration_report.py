"""Language-agnostic build + test report for a finished workspace.

After an agent run (simple chat OR team/parallel), compile (build) and run the
project's tests via ``project_runner`` — which already knows Python / Node /
Go / Rust / Maven / Gradle / CMake — and format a human report. When the
toolchain isn't installed here (or there's no recognised project), degrade to
STEP-BY-STEP manual instructions so the user can run the checks themselves.

Public surface:
    build_and_test_report(cwd) -> {"ok": bool | None, "md": str}
      ok = True (green) / False (build or tests failed) / None (couldn't run
      here — see the manual steps in ``md``).
"""
from __future__ import annotations

import os

from aiforge_core.config import languages as _languages

# stderr fragments that mean the toolchain isn't installed here (→ give the
# user manual steps instead of reporting a false failure).
_TOOLCHAIN_ABSENT = (
    "command not found", "not found", "no such file", "is not recognized",
    "not installed", "could not find", "unable to locate", "no such command",
)

# language → ordered manual "set up + test" commands.
_MANUAL: dict[str, list[str]] = {
    "python": [
        "python3 -m venv .venv && . .venv/bin/activate",
        "pip install -e .   # or: pip install -r requirements.txt",
        "pip install pytest && pytest -q",
    ],
    "node": ["npm install", "npm test"],
    "java-maven": ["mvn -q compile", "mvn -q test"],
    "java-gradle": ["./gradlew build", "./gradlew test"],
    "go": ["go build ./...", "go test ./..."],
    "rust": ["cargo build", "cargo test"],
    "c/c++": [
        "cmake -S . -B build && cmake --build build   # or: make",
        "ctest --test-dir build   # or run the produced test binary",
    ],
    "shell": [
        "bash -n *.sh          # syntax check",
        "shellcheck *.sh       # lint (if installed)",
        "bats tests/           # if you use the bats test framework",
    ],
    "ruby": ["bundle install", "bundle exec rspec   # or: ruby -Itest test/*.rb"],
    "php": ["composer install", "./vendor/bin/phpunit"],
}

# Kotlin (first-class) — build/test steps sourced from the language registry
# (aiforge_core/config/languages/kotlin.py) so they stay in sync with the rest
# of the subsystem. Additive: no existing key changes.
_kt_profile = _languages.PROFILES["kotlin"]
_MANUAL["kotlin"] = [
    f"{_kt_profile.compile_cmd}   # build (or: mvn -q compile)",
    f"{_kt_profile.test_cmd}   # test (or: mvn -q test)",
]


# Map the project_runner stack name → our _MANUAL language key, so the manual
# steps ALWAYS match the stack that was actually tested (no re-detection race).
_STACK_TO_LANG = {
    "python": "python", "node": "node", "javascript": "node",
    "typescript": "node", "java": "java-maven", "maven": "java-maven",
    "gradle": "java-gradle", "go": "go", "golang": "go", "rust": "rust",
    "cpp": "c/c++", "c": "c/c++", "php": "php", "ruby": "ruby", "shell": "shell",
    "kotlin": "kotlin",
}


# Authoritative marker files → language, most-specific first.
_LANG_MARKERS = (
    (("pom.xml",), "java-maven"),
    (("build.gradle", "build.gradle.kts", "settings.gradle"), "java-gradle"),
    (("go.mod",), "go"),
    (("Cargo.toml",), "rust"),
    (("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"), "python"),
    (("package.json",), "node"),
    (("composer.json",), "php"),
    (("Gemfile",), "ruby"),
    (("CMakeLists.txt", "Makefile"), "c/c++"),
)

# Extension fallback in FIXED priority (python before node so a stray .js can't
# shadow a Python project; kotlin last so nothing already-matched changes).
_LANG_EXTS = (
    ("python", {".py"}), ("go", {".go"}), ("rust", {".rs"}),
    ("java-maven", {".java"}), ("c/c++", {".c", ".cpp", ".cc", ".cxx"}),
    ("php", {".php"}), ("ruby", {".rb"}),
    ("node", {".ts", ".tsx", ".js", ".mjs"}), ("shell", {".sh", ".bash"}),
    ("kotlin", {".kt", ".kts"}),
)


def _detect_lang(cwd: str) -> str | None:
    """Best-effort language. MARKER files win (a pyproject/go.mod/Cargo.toml is
    authoritative); only when there's no marker do we fall back to extensions, in
    a fixed priority so a stray .js can't shadow a Python project (the bug that
    showed 'npm install' for a pytest project)."""
    for names, lang in _LANG_MARKERS:
        if any(os.path.exists(os.path.join(cwd, n)) for n in names):
            return lang
    exts: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
            ".aiforge-worktrees", "__pycache__")]
        for f in files:
            exts.add(os.path.splitext(f)[1].lower())
    for lang, es in _LANG_EXTS:
        if exts & es:
            return lang
    return None


def _manual_steps_md(cwd: str, lang: str | None = None) -> str:
    """Manual build/test steps. ``lang`` (from the stack actually tested) wins over
    re-detection so the steps never contradict the run."""
    lang = lang or _detect_lang(cwd)
    steps = _MANUAL.get(lang or "")
    if not steps:
        return "_No recognised project — add a build/test setup to enable auto-checks._"
    body = "\n".join(f"{i + 1}. `{s}`" for i, s in enumerate(steps))
    return f"**▶ To build & test it yourself ({lang}):**\n{body}"


def _absent(err: str) -> bool:
    low = (err or "").lower()
    return any(m in low for m in _TOOLCHAIN_ABSENT)


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


_IMPORT_SCAN_SKIP = (".git", ".venv", ".aiforge-venv", "node_modules",
                     "__pycache__", ".aiforge-worktrees")


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
    ".aiforge-venv", ".aiforge-worktrees", ".venv", "venv", "env",
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


def _ensure_pytest_venv(cwd: str, venv: str, py: str, timeout: int) -> None:
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
        return
    if not os.path.exists(py):
        subprocess.run([sys.executable, "-m", "venv", venv],
                       capture_output=True, timeout=120)
    # CORE test deps FIRST, in their own call that MUST succeed — the pytest
    # plugins models reference via pyproject addopts (cov, asyncio, mock) so a
    # `--cov`/`@pytest.mark.asyncio` config doesn't exit with "unrecognized
    # arguments" and zero signal.
    subprocess.run([py, "-m", "pip", "-q", "install", "pytest", "pytest-cov",
                    "pytest-asyncio", "pytest-mock", "pytest-timeout", "ruff"],
                   capture_output=True, timeout=timeout)
    # Third-party imports BEST-EFFORT and one at a time — a single
    # unresolvable/mis-detected name (a stray stdlib module, a private package)
    # must NOT abort the whole install and strand pytest. Each failure is
    # isolated; a genuinely-missing import just surfaces as a real test error.
    for dep in _third_party_imports(cwd):
        subprocess.run([py, "-m", "pip", "-q", "install", dep],
                       capture_output=True, timeout=timeout)
    req = os.path.join(cwd, "requirements.txt")
    if os.path.exists(req):
        subprocess.run([py, "-m", "pip", "-q", "install", "-r", req],
                       capture_output=True, timeout=timeout)


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
    if not _python_test_files(cwd):
        return None
    venv = os.path.join(cwd, ".aiforge-venv")
    py = os.path.join(venv, "bin", "python")
    try:
        _ensure_pytest_venv(cwd, venv, py, timeout)
        env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        rc, out = _run_pytest_capturing(py, cwd, env, timeout)
        ok = rc == 0
        # LINT gate (Python leg): when tests otherwise PASS, run the real-bug ruff
        # codes via the managed venv's ruff. The generic multi-language dispatch
        # below handles other stacks.
        if ok and os.environ.get("AIFORGE_LINT_GATE", "1") not in ("0", "false"):
            lok, lout = _static_lint_python(cwd, py, env)
            if not lok:
                ok = False
                out += lout
        return ok, out[-4000:]
    except Exception:  # noqa: BLE001
        return None


# Native static checker per language — the "easy way": reuse each toolchain's own
# linter/typechecker (no heavy universal dep). Real-bug level only, never style.
def _static_lint_python(cwd, py, env):
    try:
        import subprocess
        lp = subprocess.run(
            [py, "-m", "ruff", "check", "--select", "F821,F822,F811",
             "--no-cache", "-q", "."], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=90)
        if lp.returncode != 0 and (lp.stdout or lp.stderr).strip():
            return False, "\n\n=== python lint (undefined/redef) — fix ===\n" + lp.stdout + lp.stderr
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def _dispatch_language_checks(cwd: str, files, has, run) -> None:
    """Run each present language's native static check via the ``run`` helper."""
    # TypeScript — the compiler IS the typecheck.
    if files(".ts", ".tsx") and os.path.exists(os.path.join(cwd, "tsconfig.json")) and has("npx"):
        run(["npx", "--yes", "tsc", "--noEmit"], "typescript typecheck", 180)
    # plain JavaScript — syntax check each file (no compiler).
    elif files(".js", ".mjs") and has("node"):
        for f in files(".js", ".mjs")[:60]:
            run(["node", "--check", f], f"js syntax {os.path.relpath(f, cwd)}", 20)
    # Go — vet catches real bugs beyond compile.
    if files(".go") and has("go"):
        run(["go", "vet", "./..."], "go vet", 120)
    # Rust — clippy if present (compile already typechecks; clippy adds real lints).
    if files(".rs") and has("cargo"):
        run(["cargo", "clippy", "--quiet"], "rust clippy", 180)


def run_static_checks(cwd: str) -> tuple[bool, str]:
    """Language-native static checks for the stacks present — catches real bugs a
    test run can miss (undefined name, type error, bad ref). Best-effort: any tool
    that isn't installed is skipped. Returns ``(ok, output)``. Off with
    ``AIFORGE_LINT_GATE=0``. Compiled langs (Java/Go/Rust/Kotlin/C) are already
    typechecked by their build step, so here we cover the interpreted/loose ones."""
    import shutil
    import subprocess
    if os.environ.get("AIFORGE_LINT_GATE", "1") in ("0", "false"):
        return True, ""
    problems: list[str] = []

    def _has(exe: str) -> bool:
        return shutil.which(exe) is not None

    def _run(cmd: list[str], label: str, timeout: int = 90) -> None:
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout)
            if p.returncode != 0 and (p.stdout or p.stderr).strip():
                problems.append(f"=== {label} ===\n{(p.stdout + p.stderr)[-1500:]}")
        except Exception:  # noqa: BLE001
            pass

    def _files(*exts):
        out = []
        for root, dirs, fs in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in _LINT_SKIP and not d.startswith(".")]
            out += [os.path.join(root, f) for f in fs if f.endswith(exts)]
        return out

    _dispatch_language_checks(cwd, _files, _has, _run)
    if problems:
        return False, "\n\n=== static checks — fix these ===\n" + "\n\n".join(problems)
    return True, ""


_LINT_SKIP = {"node_modules", "venv", ".venv", "__pycache__", "target", "build",
              "dist", ".git", ".aiforge-venv", "vendor"}


def _bare_python_report(cwd: str) -> "dict | None":
    """The report for a bare Python tree (no build marker) WITH tests — run
    pytest via a managed venv so we still report real pass/fail. None when there
    is nothing to run here."""
    bare = run_bare_python_tests(cwd)
    if bare is None:
        return None
    ok, output = bare
    # We ran pytest → the manual steps are PYTHON, not a re-detection race.
    out = ["## Integration check — **python (pytest, no build marker)**", "",
           f"- **tests (end-to-end):** {'✅ passed' if ok else '❌ failed'}"]
    if not ok and output:
        out.append("```\n" + output[-1400:] + "\n```")
    out += ["", _manual_steps_md(cwd, "python")]
    return {"ok": ok, "md": "\n".join(out)}


def _run_build_step(cwd: str, project, manual: str,
                    out: list) -> "tuple[bool, dict | None]":
    """Append the build/compile result to ``out``. Returns
    ``(build_ok, short_circuit_report)`` — the report is set (and build_ok is
    False) only when the build toolchain is absent (can't auto-build)."""
    build = project(action="build", cwd=cwd) or {}
    berr = str(build.get("error") or "")
    if not build.get("ok") and _absent(berr):
        out += ["⚠ Build toolchain isn't installed on this host — can't auto-build.",
                "", manual]
        return False, {"ok": None, "md": "\n".join(out)}
    out.append(f"- **build/compile:** {'✅ passed' if build.get('ok') else '❌ failed'}")
    if not build.get("ok") and berr:
        out.append("```\n" + berr[-1000:] + "\n```")
    return bool(build.get("ok")), None


def _run_test_step(cwd: str, stacks, has_tests, project, manual: str,
                   build_ok: bool, out: list) -> "tuple[bool, dict | None]":
    """Append the test result to ``out``, gating strictly on tests when present.
    Returns ``(ok, short_circuit_report)`` — the report is set only when the test
    toolchain is absent."""
    if not has_tests(cwd, stacks):
        out.append("- **tests:** _none found_ — add tests to verify behaviour.")
        return build_ok, None
    test = project(action="test", cwd=cwd) or {}
    terr = str(test.get("error") or test.get("output") or "")
    if not test.get("ok") and _absent(terr):
        out += ["", "⚠ Test toolchain isn't installed on this host.", "", manual]
        return build_ok, {"ok": None, "md": "\n".join(out)}
    ok = bool(test.get("ok"))
    out.append(f"- **tests (end-to-end):** {'✅ passed' if ok else '❌ failed'}")
    if not ok and terr:
        out.append("```\n" + terr[-1400:] + "\n```")
    return ok, None


def build_and_test_report(cwd: str) -> dict:
    """Compile + test ``cwd`` and return ``{"ok", "md"}``. ``ok`` is None when
    the checks couldn't run here (toolchain absent / no project) — ``md`` then
    carries step-by-step manual instructions."""
    manual = _manual_steps_md(cwd)
    try:
        from aiforge_core.runtime.tools.project_runner import (
            _has_tests, detect, project,
        )
    except Exception:  # noqa: BLE001
        return {"ok": None, "md": "## Integration check\n\n" + manual}

    stacks = (detect(cwd) or {}).get("stacks") or []
    if not stacks:
        bare = _bare_python_report(cwd)
        if bare is not None:
            return bare
        return {"ok": None, "md": "## Integration check\n\nNo build markers "
                "found here.\n\n" + manual}

    # Manual steps must match the stack we actually detected/tested, not a
    # separate _detect_lang pass (which raced .js over .py → wrong 'npm install').
    manual = _manual_steps_md(cwd, _STACK_TO_LANG.get(str(stacks[0]).lower()))
    out = [f"## Integration check — detected: **{', '.join(stacks)}**", ""]

    build_ok, short = _run_build_step(cwd, project, manual, out)
    if short is not None:
        return short
    ok, short = _run_test_step(cwd, stacks, _has_tests, project, manual,
                               build_ok, out)
    if short is not None:
        return short
    out += ["", manual]      # always show how the user can re-run it themselves
    return {"ok": ok, "md": "\n".join(out)}


__all__ = ["build_and_test_report"]
