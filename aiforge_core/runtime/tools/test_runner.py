"""Test runner tool (standards gap C9).

Wraps pytest / `mvn test` / `npm test` / `cargo test` as a first-class
Doer tool so cheap modes (`--lf`, `-k`, discover-only) are accessible
without bash improvisation.

KISS: pick test command by project marker; pass `mode` + `pattern`
through to the underlying tool.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any

from aiforge_core.runtime.sandbox import root

log = logging.getLogger("aiforge.tools.test_runner")


def build_test_command(
    base_cmd: list[str],
    *,
    framework: str,
    parallel: bool,
    workers: int | None,
) -> list[str]:
    """Append the right parallel/shard flag for ``framework`` (gap A8a).

    Pure string builder — never mutates ``base_cmd`` and never requires
    the underlying plugin (e.g. pytest-xdist) to be installed.

    - python (pytest) → ``-n auto`` (workers None) or ``-n <workers>``
    - go               → ``-parallel <workers>`` (default 4)
    - node (jest)      → ``--maxWorkers=<workers>`` (default 4)
    - java-maven, rust, anything else → unchanged (no-op)

    When ``parallel`` is False, returns ``base_cmd`` unchanged.
    """
    if not parallel:
        return base_cmd
    cmd = list(base_cmd)
    if framework == "python":
        cmd += ["-n", "auto" if workers is None else str(workers)]
    elif framework == "go":
        cmd += ["-parallel", str(workers if workers else 4)]
    elif framework == "node":
        cmd.append(f"--maxWorkers={workers if workers else 4}")
    # java-maven / java-gradle / rust / unknown → no-op
    return cmd


def _pick_runner() -> tuple[str, str] | None:
    """Return (language, base_command). First marker hit wins."""
    repo = root()
    table = [
        ("pyproject.toml", "python",       "pytest"),
        ("setup.py",       "python",       "pytest"),
        ("package.json",   "node",         "npm"),
        ("pom.xml",        "java-maven",   "mvn"),
        ("build.gradle",   "java-gradle",  "./gradlew"),
        ("Cargo.toml",     "rust",         "cargo"),
        ("go.mod",         "go",           "go"),
    ]
    for marker, lang, tool in table:
        if (repo / marker).is_file():
            return lang, tool
    return None


def run_tests(
    mode: str = "fast",
    pattern: str = "",
) -> dict[str, Any]:
    """Run the project's tests.

    Args:
        mode: ``fast`` (default; ``--lf`` or ``--testFailedFirst`` when
            supported), ``all`` (run the full suite), ``discover``
            (collect-only — confirm tests exist, no execution).
        pattern: optional ``-k`` / ``--testNamePattern`` filter. Pytest
            and Jest accept it; other runners ignore.

    Returns ``{ok, language, exit_code, stdout, stderr, mode}``.
    Soft-failure modes: ``no_language`` / ``missing_tool`` /
    ``timeout``.
    """
    pick = _pick_runner()
    if pick is None:
        return {"ok": False, "error": "no_language"}
    lang, tool = pick
    if shutil.which(tool.split("/")[-1]) is None and \
            shutil.which(tool) is None:
        return {"ok": False, "error": "missing_tool", "tool": tool}

    cmd: list[str]
    if lang == "python":
        cmd = [tool, "-q", "--no-header"]
        if mode == "fast":
            cmd.append("--lf")
        elif mode == "discover":
            cmd = [tool, "--collect-only", "-q"]
        if pattern:
            cmd += ["-k", pattern]
    elif lang == "node":
        cmd = [tool, "test", "--silent"]
        if mode == "discover":
            cmd += ["--", "--listTests"]
        elif mode == "fast":
            cmd += ["--", "--onlyFailures"]
        if pattern:
            cmd += ["--", "--testNamePattern", pattern]
    elif lang in {"java-maven"}:
        cmd = [tool, "-q", "test"]
        if pattern:
            cmd += [f"-Dtest={pattern}"]
        if mode == "discover":
            cmd = [tool, "-q", "test", "-DskipTests"]
    elif lang == "java-gradle":
        cmd = [tool, "test"]
        if pattern:
            cmd += ["--tests", pattern]
    elif lang == "rust":
        cmd = [tool, "test", "--quiet"]
        if mode == "discover":
            cmd = [tool, "test", "--", "--list"]
        if pattern:
            cmd.append(pattern)
    elif lang == "go":
        cmd = [tool, "test", "./..."]
        if mode == "discover":
            cmd = [tool, "test", "./...", "-list", ".*"]
        if pattern:
            cmd += ["-run", pattern]
    else:
        return {"ok": False, "error": "no_language"}

    # Opt-in parallel/shard mode (gap A8a). Default off → unchanged.
    if os.environ.get("AIFORGE_TEST_PARALLEL") == "1" and mode != "discover":
        workers_env = os.environ.get("AIFORGE_TEST_WORKERS")
        workers = int(workers_env) if (workers_env or "").isdigit() else None
        cmd = build_test_command(
            cmd, framework=lang, parallel=True, workers=workers,
        )

    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900,
            cwd=str(root()),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "language": lang}
    return {
        "ok": p.returncode == 0,
        "language": lang,
        "mode": mode,
        "exit_code": p.returncode,
        "stdout": p.stdout[-4000:],
        "stderr": p.stderr[-4000:],
    }


__all__ = ["run_tests", "build_test_command"]
