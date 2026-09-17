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

from ._report_pytest import (  # noqa: F401  # re-exported
    _AIFORGE_VENV,
    _VENV,
    _WORKTREES,
    _IMPORT_SCAN_SKIP,
    _STDLIB,
    _TEST_SKIP_DIRS,
    _ensure_pytest_venv,
    _imported_modules,
    _local_names,
    _pytest_timeout_args,
    _python_test_files,
    _run_pytest_capturing,
    _stdlib_names,
    _third_party_imports,
    run_bare_python_tests,
)

_CPP = 'c/c++'

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
    _CPP: [
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
    "cpp": _CPP, "c": _CPP, "php": "php", "ruby": "ruby", "shell": "shell",
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
    (("CMakeLists.txt", "Makefile"), _CPP),
)

# Extension fallback in FIXED priority (python before node so a stray .js can't
# shadow a Python project; kotlin last so nothing already-matched changes).
_LANG_EXTS = (
    ("python", {".py"}), ("go", {".go"}), ("rust", {".rs"}),
    ("java-maven", {".java"}), (_CPP, {".c", ".cpp", ".cc", ".cxx"}),
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
            ".git", _VENV, "venv", "node_modules", "target", "build", "dist",
            _WORKTREES, "__pycache__")]
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


_LINT_SKIP = {"node_modules", "venv", _VENV, "__pycache__", "target", "build",
              "dist", ".git", _AIFORGE_VENV, "vendor"}


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
            _has_tests,
            detect,
            project,
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
