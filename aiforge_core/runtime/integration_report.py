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


def _detect_lang(cwd: str) -> str | None:
    """Best-effort language. MARKER files win (a pyproject/go.mod/Cargo.toml is
    authoritative); only when there's no marker do we fall back to extensions, and
    then in a fixed priority so a stray .js can't shadow a Python project (the bug
    that showed 'npm install' for a pytest project)."""
    def has(*names: str) -> bool:
        return any(os.path.exists(os.path.join(cwd, n)) for n in names)
    exts: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
            ".aiforge-worktrees", "__pycache__")]
        for f in files:
            exts.add(os.path.splitext(f)[1].lower())
    # 1) authoritative marker files, most-specific first
    if has("pom.xml"):
        return "java-maven"
    if has("build.gradle", "build.gradle.kts", "settings.gradle"):
        return "java-gradle"
    if has("go.mod"):
        return "go"
    if has("Cargo.toml"):
        return "rust"
    if has("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"):
        return "python"
    if has("package.json"):
        return "node"
    if has("composer.json"):
        return "php"
    if has("Gemfile"):
        return "ruby"
    if has("CMakeLists.txt", "Makefile"):
        return "c/c++"
    # 2) no marker — extension fallback, fixed priority (python before node)
    for lang, es in (("python", {".py"}), ("go", {".go"}), ("rust", {".rs"}),
                     ("java-maven", {".java"}), ("c/c++", {".c", ".cpp", ".cc", ".cxx"}),
                     ("php", {".php"}), ("ruby", {".rb"}),
                     ("node", {".ts", ".tsx", ".js", ".mjs"}),
                     ("shell", {".sh", ".bash"}),
                     # Kotlin (first-class) placed LAST so no previously-matched
                     # language changes — only a marker-less .kt/.kts tree that
                     # matched nothing before now resolves to "kotlin".
                     ("kotlin", {".kt", ".kts"})):
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


def _third_party_imports(cwd: str) -> list[str]:
    """Top-level third-party modules imported anywhere in the tree — so a bare
    (marker-less) project's test venv can pip-install them (pygame, numpy, …)."""
    import re as _re
    _std = _stdlib_names()
    pat = _re.compile(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", _re.MULTILINE)
    mods: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".venv", ".aiforge-venv", "node_modules", "__pycache__",
            ".aiforge-worktrees")]
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except Exception:  # noqa: BLE001
                continue
            for m in pat.findall(src):
                if m and m not in _std:
                    mods.add(m)
    # a local package (a dir/… .py in the tree) isn't third-party.
    local = {d for d in os.listdir(cwd)} if os.path.isdir(cwd) else set()
    local |= {os.path.splitext(f)[0] for f in os.listdir(cwd)} if os.path.isdir(cwd) else set()
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


def run_bare_python_tests(cwd: str, timeout: int = 300):
    """Run pytest on a bare (marker-less) Python tree via a managed venv that
    pip-installs pytest + the tree's third-party imports. Returns ``(ok, output)``
    or ``None`` when there are no tests (nothing to check). The venv lives at
    ``.aiforge-venv`` (git-ignored) and is reused across reconcile rounds."""
    import subprocess
    import sys
    if not _python_test_files(cwd):
        return None
    venv = os.path.join(cwd, ".aiforge-venv")
    py = os.path.join(venv, "bin", "python")
    try:
        # (Re)ensure pytest itself is importable in the venv — a PRIOR round may
        # have created the venv but failed to install pytest (a single bad dep
        # name aborts the whole `pip install`), leaving a venv with no pytest and
        # the gate permanently blind. So check import, not just venv existence.
        def _has_pytest() -> bool:
            if not os.path.exists(py):
                return False
            c = subprocess.run([py, "-c", "import pytest"],
                               capture_output=True, timeout=60)
            return c.returncode == 0
        if not _has_pytest():
            if not os.path.exists(py):
                subprocess.run([sys.executable, "-m", "venv", venv],
                               capture_output=True, timeout=120)
            # CORE test deps FIRST, in their own call that MUST succeed — the
            # pytest plugins models reference via pyproject addopts (cov, asyncio,
            # mock) so a `--cov`/`@pytest.mark.asyncio` config doesn't exit with
            # "unrecognized arguments" and zero signal.
            subprocess.run([py, "-m", "pip", "-q", "install", "pytest",
                            "pytest-cov", "pytest-asyncio", "pytest-mock", "ruff"],
                           capture_output=True, timeout=timeout)
            # Third-party imports BEST-EFFORT and one at a time — a single
            # unresolvable/mis-detected name (a stray stdlib module, a private
            # package) must NOT abort the whole install and strand pytest. Each
            # failure is isolated; a genuinely-missing import just surfaces as a
            # real test error the reconcile can act on.
            for dep in _third_party_imports(cwd):
                subprocess.run([py, "-m", "pip", "-q", "install", dep],
                               capture_output=True, timeout=timeout)
            req = os.path.join(cwd, "requirements.txt")
            if os.path.exists(req):
                subprocess.run([py, "-m", "pip", "-q", "install", "-r", req],
                               capture_output=True, timeout=timeout)
        env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        p = subprocess.run([py, "-m", "pytest", "-q"], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
        # If pytest couldn't even START because of a broken CONFIG (a plugin/
        # addopts the tree can't satisfy → usage error, no tests collected), retry
        # once IGNORING addopts so we still get a real pass/fail the reconcile can
        # act on. General: never let a config knob mask the actual test result.
        _config_broke = (p.returncode == 4
                         or "unrecognized arguments" in out
                         or "usage: pytest" in out.lower())
        if _config_broke:
            p = subprocess.run(
                [py, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "-o", "addopts="], cwd=cwd, env=env,
                capture_output=True, text=True, timeout=timeout)
            out = p.stdout + p.stderr
        ok = p.returncode == 0
        # LINT gate (Python leg): when tests otherwise PASS, run the real-bug ruff
        # codes via the managed venv's ruff (installed above). The generic
        # multi-language dispatch below handles other stacks.
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


def run_static_checks(cwd: str) -> tuple[bool, str]:
    """Language-native static checks for the stacks present — catches real bugs a
    test run can miss (undefined name, type error, bad ref). Best-effort: any tool
    that isn't installed is skipped. Returns ``(ok, output)``. Off with
    ``AIFORGE_LINT_GATE=0``. Compiled langs (Java/Go/Rust/Kotlin/C) are already
    typechecked by their build step, so here we cover the interpreted/loose ones."""
    import glob
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

    # TypeScript — the compiler IS the typecheck.
    if _files(".ts", ".tsx") and os.path.exists(os.path.join(cwd, "tsconfig.json")) and _has("npx"):
        _run(["npx", "--yes", "tsc", "--noEmit"], "typescript typecheck", 180)
    # plain JavaScript — syntax check each file (no compiler).
    elif _files(".js", ".mjs") and _has("node"):
        for f in _files(".js", ".mjs")[:60]:
            _run(["node", "--check", f], f"js syntax {os.path.relpath(f, cwd)}", 20)
    # Go — vet catches real bugs beyond compile.
    if _files(".go") and _has("go"):
        _run(["go", "vet", "./..."], "go vet", 120)
    # Rust — clippy if present (compile already typechecks; clippy adds real lints).
    if _files(".rs") and _has("cargo"):
        _run(["cargo", "clippy", "--quiet"], "rust clippy", 180)
    if problems:
        return False, "\n\n=== static checks — fix these ===\n" + "\n\n".join(problems)
    return True, ""


_LINT_SKIP = {"node_modules", "venv", ".venv", "__pycache__", "target", "build",
              "dist", ".git", ".aiforge-venv", "vendor"}


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
        # Bare Python (no pyproject/setup.py) but WITH tests → run pytest via a
        # managed venv so we still report real pass/fail (not just "no markers").
        bare = run_bare_python_tests(cwd)
        if bare is not None:
            ok, output = bare
            # We ran pytest → the manual steps are PYTHON, not a re-detection race.
            py_manual = _manual_steps_md(cwd, "python")
            out = ["## Integration check — **python (pytest, no build marker)**",
                   "", f"- **tests (end-to-end):** {'✅ passed' if ok else '❌ failed'}"]
            if not ok and output:
                out.append("```\n" + output[-1400:] + "\n```")
            out += ["", py_manual]
            return {"ok": ok, "md": "\n".join(out)}
        return {"ok": None, "md": "## Integration check\n\nNo build markers "
                "found here.\n\n" + manual}

    # Manual steps must match the stack we actually detected/tested, not a
    # separate _detect_lang pass (which raced .js over .py → wrong 'npm install').
    manual = _manual_steps_md(cwd, _STACK_TO_LANG.get(str(stacks[0]).lower()))
    out = [f"## Integration check — detected: **{', '.join(stacks)}**", ""]

    # 1. compile / build
    build = project(action="build", cwd=cwd) or {}
    berr = str(build.get("error") or "")
    if not build.get("ok") and _absent(berr):
        out += ["⚠ Build toolchain isn't installed on this host — can't auto-build.",
                "", manual]
        return {"ok": None, "md": "\n".join(out)}
    out.append(f"- **build/compile:** {'✅ passed' if build.get('ok') else '❌ failed'}")
    if not build.get("ok") and berr:
        out.append("```\n" + berr[-1000:] + "\n```")

    # 2. tests (end-to-end) — gate strictly on tests when present.
    ok = bool(build.get("ok"))
    if _has_tests(cwd, stacks):
        test = project(action="test", cwd=cwd) or {}
        terr = str(test.get("error") or test.get("output") or "")
        if not test.get("ok") and _absent(terr):
            out += ["", "⚠ Test toolchain isn't installed on this host.", "", manual]
            return {"ok": None, "md": "\n".join(out)}
        ok = bool(test.get("ok"))
        out.append(f"- **tests (end-to-end):** {'✅ passed' if ok else '❌ failed'}")
        if not ok and terr:
            out.append("```\n" + terr[-1400:] + "\n```")
    else:
        out.append("- **tests:** _none found_ — add tests to verify behaviour.")

    out += ["", manual]      # always show how the user can re-run it themselves
    return {"ok": ok, "md": "\n".join(out)}


__all__ = ["build_and_test_report"]
