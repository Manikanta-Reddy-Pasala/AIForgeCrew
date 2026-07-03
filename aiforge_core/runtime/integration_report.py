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


def _detect_lang(cwd: str) -> str | None:
    """Best-effort project language from marker files + file extensions."""
    def has(*names: str) -> bool:
        return any(os.path.exists(os.path.join(cwd, n)) for n in names)
    exts: set[str] = set()
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in (
            ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
            ".aiforge-worktrees", "__pycache__")]
        for f in files:
            exts.add(os.path.splitext(f)[1].lower())
    if has("pom.xml"):
        return "java-maven"
    if has("build.gradle", "build.gradle.kts", "settings.gradle"):
        return "java-gradle"
    if has("go.mod") or ".go" in exts:
        return "go"
    if has("Cargo.toml") or ".rs" in exts:
        return "rust"
    if has("package.json") or ".ts" in exts or ".js" in exts:
        return "node"
    if has("CMakeLists.txt", "Makefile") or exts & {".c", ".cpp", ".cc", ".cxx"}:
        return "c/c++"
    if has("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt") or ".py" in exts:
        return "python"
    if has("composer.json") or ".php" in exts:
        return "php"
    if has("Gemfile") or ".rb" in exts:
        return "ruby"
    if ".sh" in exts or ".bash" in exts:
        return "shell"
    return None


def _manual_steps_md(cwd: str) -> str:
    lang = _detect_lang(cwd)
    steps = _MANUAL.get(lang or "")
    if not steps:
        return "_No recognised project — add a build/test setup to enable auto-checks._"
    body = "\n".join(f"{i + 1}. `{s}`" for i, s in enumerate(steps))
    return f"**▶ To build & test it yourself ({lang}):**\n{body}"


def _absent(err: str) -> bool:
    low = (err or "").lower()
    return any(m in low for m in _TOOLCHAIN_ABSENT)


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
        return {"ok": None, "md": "## Integration check\n\nNo build markers "
                "found here.\n\n" + manual}

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
