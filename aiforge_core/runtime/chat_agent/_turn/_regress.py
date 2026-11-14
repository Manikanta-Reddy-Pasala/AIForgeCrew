"""Regression watch: tests that passed before this turn's edits and fail now.

A small model checks its work with narrow test runs (one file, ``-k``, one
test id). When an edit breaks something elsewhere, nothing it runs shows it:
tomlkit (core-mode bench) reached 5 hidden tests, then a change for one part
broke the round trip of inline tables, 25 existing tests failed and the score
went to 0 while every test the model ran passed — in 3 of 7 runs.

So after a pytest run OR an edit, once at least ``AIFORGE_CHAT_REGRESS_EDITS``
(3) edits have landed since the last check, the harness runs the WHOLE suite
with the model's own command (narrowing stripped; or the repo's saved test
command; or the python its probes use with ``-m pytest`` — in one tomlkit run
the model never ran pytest at all, 258 calls of ``python -c`` probes) and compares the failing tests
with the same command run on the work's base commit (a temporary git
worktree of HEAD, made once). Tests that fail now and did not fail at the
base are named on that tool result. Nothing is said when there are none.

Bounded: a suite slower than ``AIFORGE_CHAT_REGRESS_MAX_S`` (90 s) on the
base turns the watch off for the turn; at most ``AIFORGE_CHAT_REGRESS_RUNS``
(8) checks a turn. Python (pytest) only. Off by default while it is measured:
``AIFORGE_CHAT_REGRESS=1`` turns it on.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import time

_PYTEST = re.compile(r"\bpytest\b")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_REGRESS", "0").strip().lower() in (
        "1", "true", "yes", "on")


def base_command(cmd: str) -> str:
    """The model's pytest command for the whole suite: no pipe, no ``-k``/
    ``-x``/``--lf``/``::`` narrowing, no single test file or folder argument."""
    cmd = cmd.split("|", 1)[0].strip()
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return ""
    out, skip, after = [], False, False
    for p in parts:
        if not after:                      # `python -m pytest`: -m here is python's
            out.append(p)
            after = os.path.basename(p) in ("pytest", "py.test")   # the token, not a path
            continue
        if p in ("&&", ";", "||"):         # a second command after pytest: stop
            break
        if skip:
            skip = False
            continue
        if p in ("-k", "-m", "--deselect", "--maxfail"):
            skip = True
            continue
        if p in ("-x", "--exitfirst", "--lf", "--last-failed", "-v", "-vv", "-s",
                 "--tb=short", "--tb=line", "--tb=no") or p.startswith(("--maxfail=", "-k=")):
            continue
        if "::" in p or p.endswith(".py") or (not p.startswith("-") and "/" in p) \
                or (not p.startswith("-") and os.path.isdir(p)):
            continue                       # one file / folder / test id: the run's, not the suite
        out.append(p)
    if not after:
        return ""
    return " ".join(shlex.quote(p) if " " in p else p for p in out)


def _failing(output: str) -> "set[str] | None":
    """Failing/erroring test ids from pytest output; None when the output has
    no summary line (the run did not finish — not comparable)."""
    if not re.search(r"\b(?:passed|failed|error|errors|no tests ran)\b[^\n]*\bin [\d.]+s", output):
        return None
    ids = set(re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", output, re.M))
    return ids


def _run(cmd: str, cwd: str, timeout: int) -> "tuple[str, float] | None":
    t0 = time.monotonic()
    try:
        p = subprocess.run(["bash", "-lc", cmd + " -q -rfE -p no:cacheprovider"],
                           cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    return (p.stdout or "") + "\n" + (p.stderr or ""), time.monotonic() - t0


def _base_failures(st, cmd: str, root: str) -> "set[str] | None":
    """The command's failing tests on HEAD, from a temporary worktree (once)."""
    cache = st.__dict__.setdefault("regress_base", {})
    if cmd in cache:
        return cache[cmd]
    cache[cmd] = None
    tmp = tempfile.mkdtemp(prefix="af-regress-")
    wt = os.path.join(tmp, "base")
    try:
        if subprocess.run(["git", "-C", root, "worktree", "add", "--detach", "-q", wt, "HEAD"],
                          capture_output=True, timeout=30).returncode != 0:
            return None
        res = _run(cmd.replace(root, wt), wt, _env_int("AIFORGE_CHAT_REGRESS_MAX_S", 90))
        if res is None:
            st.regress_off = True                   # too slow (or broken) to watch
            return None
        out, secs = res
        if secs > _env_int("AIFORGE_CHAT_REGRESS_MAX_S", 90):
            st.regress_off = True
            return None
        cache[cmd] = _failing(out)
        return cache[cmd]
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt],
                       capture_output=True, timeout=30)
        try:
            os.rmdir(tmp)
        except OSError:
            pass


_EDIT_TOOLS = ("file_patch", "multi_edit", "file_write", "edit", "apply_patch")
_PY_BIN = re.compile(r"(?:^|[\s;&(])((?:\S*/)?python3?(?:\.\d+)?)\s")


def _note_interpreter(st, raw: str, result) -> None:
    """Remember the python the model's commands run with (a test run, or a
    `python -c` probe that worked) — the suite is run with the same one."""
    if not isinstance(result, dict) or result.get("ok") is False:
        return
    if _PYTEST.search(raw):
        b = base_command(raw)
        if b:
            st.regress_cmd = b
    m = _PY_BIN.search(" " + raw + " ")
    if m:
        st.regress_py = m.group(1)


def _suite_command(st, root: str) -> str:
    """The whole-suite command: the model's own pytest command, the repo's
    saved test command, or its python with ``-m pytest`` (a repo with tests)."""
    if getattr(st, "regress_cmd", ""):
        return st.regress_cmd
    from aiforge_core.runtime import ops_memory
    recipe = ops_memory.load()["repos"].get(ops_memory._key(os.path.basename(root)), {})
    b = base_command(recipe.get("test_cmd") or "")
    if b:
        return b
    if not any(os.path.isdir(os.path.join(root, d)) for d in ("tests", "test")):
        return ""
    py = getattr(st, "regress_py", "") or recipe.get("python") or "python3"
    return f"cd {shlex.quote(root)} && {py} -m pytest"


def check(st, name: str, args: dict, result, cwd: str) -> str:
    """After a pytest run or an edit, once enough edits landed since the last
    check: the note naming tests this turn's edits broke, or ""."""
    if not enabled() or getattr(st, "regress_off", False) or not isinstance(result, dict):
        return ""
    raw = str((args or {}).get("cmd") or (args or {}).get("command") or "")
    if name == "run_command":
        _note_interpreter(st, raw, result)
    is_test = name == "run_tests" or (name == "run_command" and _PYTEST.search(raw))
    if not is_test and not (name in _EDIT_TOOLS and result.get("ok") is not False):
        return ""                 # the model never running pytest is the case this is for
    if result.get("running") or result.get("background"):
        return ""
    edits = int(getattr(st, "edits_made", 0) or 0)
    if edits - int(getattr(st, "regress_at", 0) or 0) < _env_int("AIFORGE_CHAT_REGRESS_EDITS", 3):
        return ""
    runs = int(getattr(st, "regress_runs", 0) or 0)
    if runs >= _env_int("AIFORGE_CHAT_REGRESS_RUNS", 8):
        return ""
    from aiforge_core.runtime import ops_memory
    root = ops_memory.repo_root(cwd)
    cmd = (base_command(raw) if is_test and raw else "") or _suite_command(st, root)
    if not cmd or not root:
        return ""
    st.regress_at = edits
    st.regress_runs = runs + 1
    base = _base_failures(st, cmd, root)
    if base is None or getattr(st, "regress_off", False):
        return ""
    res = _run(cmd, root, _env_int("AIFORGE_CHAT_REGRESS_MAX_S", 90) * 2)
    if res is None:
        return ""
    now = _failing(res[0])
    if now is None:
        return ""
    new = sorted(now - base)
    said = st.__dict__.setdefault("regress_said", set())
    if not new or set(new) <= said:
        return ""
    said.update(new)
    shown = ", ".join(new[:8]) + (f" (+{len(new) - 8} more)" if len(new) > 8 else "")
    return (f"[automated regression check — not the user] {len(new)} test(s) that passed "
            f"before your edits now fail: {shown}. The full suite (`{cmd}`) shows them; "
            "your narrow run did not. Fix them before moving on.")
