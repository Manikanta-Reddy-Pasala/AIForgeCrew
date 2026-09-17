"""Checking and testing a workflow's scripts before they are written, and
mirroring the workflow to memory."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from aiforge_core.runtime import skills as _sk


def _pkg():
    """``workflows``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``workflows``; patch any other
    name on this module."""
    import aiforge_core.runtime.workflows as package
    return package


_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _proc_error(r, label: str) -> str | None:
    if r.returncode == 0:
        return None
    return (r.stderr or r.stdout or f"{label} failed").strip()[:500]


def _py_syntax_error(path: Path) -> str | None:
    import py_compile
    try:
        py_compile.compile(str(path), doraise=True)
        return None
    except py_compile.PyCompileError as exc:
        return str(exc)[:500]


def _check_script_syntax(path: Path) -> str | None:
    """Static syntax check for a helper script — returns an error string or
    None. Best-effort per language: bash -n for shell, py_compile for python,
    node --check for js when node exists. Unknown extensions pass (there is no
    checker to run). A missing/broken checker must not block authoring."""
    ext = path.suffix.lower()
    try:
        if ext in (".sh", ".bash"):
            return _proc_error(
                subprocess.run(["bash", "-n", str(path)], capture_output=True,
                               text=True, timeout=15), "bash -n")
        if ext == ".py":
            return _py_syntax_error(path)
        if ext in (".js", ".mjs") and shutil.which("node"):
            return _proc_error(
                subprocess.run(["node", "--check", str(path)],
                               capture_output=True, text=True, timeout=15),
                "node --check")
    except Exception:  # noqa: BLE001
        return None
    return None


def _normalize_scripts(scripts) -> tuple[list[tuple[str, str, str]], str | None]:
    """Validate the ``scripts`` argument into ``[(filename, content, test)]``.
    Accepts a list of {name, content, test?} dicts or a {name: content}
    mapping. ``test`` is the shell command the HARD gate runs to prove the
    script works ("" → run the script itself; "skip" → explicitly untestable,
    e.g. needs prod-only state). Rejects path traversal (any separator in the
    name) and empty content."""
    if not scripts:
        return [], None
    if isinstance(scripts, dict):
        scripts = [{"name": k, "content": v} for k, v in scripts.items()]
    if not isinstance(scripts, list):
        return [], "scripts must be a list of {name, content}"
    out: list[tuple[str, str, str]] = []
    for s in scripts:
        if not isinstance(s, dict):
            return [], "each script must be a {name, content} object"
        fname = str(s.get("name") or s.get("filename") or "").strip()
        content = s.get("content") or s.get("body") or ""
        test = str(s.get("test") or "").strip()
        if not fname or not _SCRIPT_NAME_RE.match(fname):
            return [], f"invalid script name {fname!r} (plain filename only, no paths)"
        if not isinstance(content, str) or not content.strip():
            return [], f"script {fname!r} has no content"
        out.append((fname, content, test))
    return out, None


_SCRIPT_RUNNER_BY_EXT = {".sh": "bash", ".bash": "bash", ".py": "python3",
                         ".js": "node", ".mjs": "node", ".rb": "ruby",
                         ".pl": "perl"}


def _script_test_cmd(fname: str, test: str) -> str | None:
    """The command that proves this script works: its declared ``test``, else
    the script itself with no args. None when there is no way to execute it
    (e.g. .sql, or the interpreter is absent) — syntax-only for those."""
    if test:
        return test
    runner = _SCRIPT_RUNNER_BY_EXT.get(Path(fname).suffix.lower())
    if runner is None:
        return None
    if runner in ("node", "ruby", "perl") and not shutil.which(runner):
        return None
    return f"{runner} {fname}"


def _run_script_test(staged_dir: Path, fname: str, cmd: str,
                     timeout: int) -> str | None:
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(staged_dir),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (f"script {fname!r} test timed out after {timeout}s "
                f"(cmd: {cmd}) — make it terminate, or give it a fast "
                "--dry-run 'test' command")
    if r.returncode == 0:
        return None
    tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-800:]
    return (f"script {fname!r} FAILED its test run (cmd: {cmd}, "
            f"exit {r.returncode}) — fix it and retry; a workflow "
            f"with a failing script is never saved:\n{tail}")


def _test_scripts_hard(staged_dir: Path,
                       script_files: list[tuple[str, str, str]]) -> str | None:
    """HARD gate (job-builder parity): actually RUN each staged script — its
    declared ``test`` command, else the script itself with no args — inside
    the staging dir. Returns an error string (with output tail) on the first
    failure; a workflow with a failing script is never saved. ``test: skip``
    opts a genuinely-untestable script out (prod-only state) — the builder
    charter requires justifying that in the body."""
    try:
        timeout = max(5, int(os.environ.get(
            "AIFORGE_WORKFLOW_SCRIPT_TEST_TIMEOUT_S", "60")))
    except ValueError:
        timeout = 60
    for fname, _content, test in script_files:
        if test.lower() == "skip":
            continue
        cmd = _script_test_cmd(fname, test)
        if cmd is None:
            continue
        err = _run_script_test(staged_dir, fname, cmd, timeout)
        if err:
            return err
    return None


def _workflow_frontmatter(name: str, description: str, trig: list,
                          scope: str) -> str:
    """OKF v0.1: ``type:`` required; ``name`` = OKF title; triggers/scope
    preserved."""
    import json as _json
    front = "---\ntype: workflow\nname: " + _json.dumps(name) + "\n"
    if description:
        front += "description: " + _json.dumps(description.strip()) + "\n"
    if trig:
        front += "triggers: [" + ", ".join(_json.dumps(t) for t in trig) + "]\n"
    return front + "scope: " + _json.dumps((scope or "global").lower()) + "\n---\n"


def _workflow_base(scope: str, cwd) -> Path:
    pkg = _pkg()
    if scope != "repo":
        return pkg._global_dir()
    root = _sk._repo_root(cwd)
    return Path(root) / ".aiforge" / "workflows" if root else pkg._global_dir()


def _vet_scripts(script_files: list) -> str:
    """Stage the scripts in a scratch dir FIRST: syntax-check each, then the
    HARD gate actually RUNS them (test command or the script itself,
    job-builder parity). Returns the error that must abort the whole write, so
    a broken workflow is never saved."""
    if not script_files:
        return ""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for fname, content, _test in script_files:
            sp = Path(td) / fname
            sp.write_text(content, encoding="utf-8")
            sp.chmod(0o755)
            serr = _check_script_syntax(sp)
            if serr:
                return (f"script {fname!r} failed its syntax check — fix and "
                        f"retry: {serr}")
        return _test_scripts_hard(Path(td), script_files) or ""


def _write_scripts(wf_dir: Path, script_files: list) -> list[str]:
    if not script_files:
        return []
    sdir = wf_dir / "scripts"
    sdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for fname, content, _test in script_files:
        dst = sdir / fname
        dst.write_text(content, encoding="utf-8")
        dst.chmod(0o755)
        paths.append(str(dst))
    return paths


def _mirror_to_memory(name: str, description: str, body: str, scope: str,
                      trig: list, cwd) -> bool:
    """Mirror into the knowledge memory (``kind=workflow``) so the workflow
    surfaces in cross-source recall."""
    try:
        from aiforge_core.runtime.tools.memory_write import memory_write as _mw
        res = _mw(text=f"WORKFLOW: {name} — {description}".strip(" —")
                  + (f"\n{body[:600]}" if body else ""),
                  kind="workflow", tags=["workflow", scope] + trig[:5],
                  decision=False, repo=_sk._repo_name(cwd))
        return bool(isinstance(res, dict) and res.get("ok", True))
    except Exception:  # noqa: BLE001
        return False
