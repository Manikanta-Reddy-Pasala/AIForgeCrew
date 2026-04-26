"""Test loop — run unit tests, parse JUnit XML failures, feed back to model.

Aider's `--test-cmd` analogue. Default Java command:
``mvn -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false test``.

Output: pass/fail summary plus the first 5 failing test signatures
with their failure messages from
``target/surefire-reports/TEST-*.xml``. Trimmed to ~4 KB.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import xml.etree.ElementTree as ET
from glob import glob

SCHEMA = {
    "type": "function",
    "function": {
        "name": "tests",
        "description": (
            "Run the unit test command (default: mvn test) and "
            "return parsed JUnit XML failures: test name + first "
            "5 failure messages. Use after patch+compile when "
            "the ticket touches behaviour."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command. Empty → uses "
                        "AIFORGE_DOER_TEST_CMD env or the "
                        "default for the worktree's lang."
                    ),
                },
            },
        },
    },
}


_DEFAULT_TEST_CMD_BY_LANG = {
    "java": "mvn -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false test",
    "python": "pytest -q",
    "ts": "npm test --silent",
}


def _resolve_command(worktree: str, override: str) -> str:
    if override:
        return override
    env = (
        os.environ.get("AIFORGE_DOER_TEST_CMD")
        or os.environ.get("AIFORGE_TEST_CMD")
    )
    if env:
        return env
    try:
        from aiforge_core.runtime import repo_standards as _rs
        repo_name = os.path.basename(os.path.normpath(worktree))
        std = _rs.get(repo_name, worktree=worktree)
        if std.test_cmd:
            return std.test_cmd
    except Exception:
        pass
    if os.path.isfile(os.path.join(worktree, "pom.xml")):
        return _DEFAULT_TEST_CMD_BY_LANG["java"]
    if os.path.isfile(os.path.join(worktree, "pyproject.toml")):
        return _DEFAULT_TEST_CMD_BY_LANG["python"]
    if os.path.isfile(os.path.join(worktree, "package.json")):
        return _DEFAULT_TEST_CMD_BY_LANG["ts"]
    return ""


def _parse_surefire(worktree: str, max_failures: int = 5) -> list[str]:
    """Walk ``target/surefire-reports/TEST-*.xml`` for failures."""
    out: list[str] = []
    for xml_path in sorted(glob(
        os.path.join(worktree, "**", "surefire-reports", "TEST-*.xml"),
        recursive=True,
    )):
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        for case in tree.iter("testcase"):
            for failure in list(case.findall("failure")) + list(case.findall("error")):
                name = f"{case.get('classname', '?')}.{case.get('name', '?')}"
                msg = (failure.get("message") or "").strip()[:300]
                out.append(f"❌ {name}\n   {msg}")
                if len(out) >= max_failures:
                    return out
    return out


def handle(worktree: str, args: dict, *, timeout_s: int = 900) -> str:
    cmd = _resolve_command(worktree, (args.get("command") or "").strip())
    if not cmd:
        return "[tests] no test command configured."
    try:
        proc = subprocess.run(
            shlex.split(cmd), cwd=worktree, capture_output=True,
            text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"[tests] timed out after {timeout_s}s; cmd: {cmd}"
    out_tail = "\n".join(
        ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()[-30:]
    )
    failures = _parse_surefire(worktree)
    status = "✅ all green" if proc.returncode == 0 else f"❌ failed rc={proc.returncode}"
    body = f"[tests] {cmd!r} → {status}"
    if failures:
        body += "\n\n## Top failures\n" + "\n\n".join(failures)
    body += "\n\n## Tail\n" + out_tail
    return body[:5000]
