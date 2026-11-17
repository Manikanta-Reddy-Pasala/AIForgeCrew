"""Failures the original code has too, for any test runner.

The finish check re-runs the turn's own test command; when it is red, the
failures may be ones the repo had before the turn touched it. A real Rust
run spent both of its final fix rounds on a test that failed on the original
code (a panic-message format difference) and ended "tests still failing".
The pytest-only check in ``_done_watch.pre_existing`` could not see it.

``same_reds_at_base`` runs the same command once on a throwaway git
worktree of the commit the work started from and says whether every failing
test of the turn's run fails there too. It only vouches when it can name
every failure (the runner's own count matches the names it read).
AIFORGE_CHAT_BASE_REDS=0 turns it off.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

#: Failing-test lines of common runners: cargo, go, Maven/Surefire, Gradle,
#: Jest/Vitest, CTest, pytest.
_NAMES = [
    re.compile(r"^test (\S+) \.\.\. FAILED\b", re.M),                       # cargo
    re.compile(r"^\s*--- FAIL: (\S+)", re.M),                                # go
    re.compile(r"^\[ERROR\]\s+([\w$]+(?:\.[\w$]+)+)(?::\d+|\s*»|\s+\w)", re.M),  # surefire summary
    re.compile(r"^(\S+ > .+?) FAILED\s*$", re.M),                            # gradle
    re.compile(r"^\s*[✕×] (.+?)(?: \(\d+(?:\.\d+)? ?m?s\))?\s*$", re.M),      # jest / vitest
    re.compile(r"^\s*\d+ - (\S+) \((?:Failed|SEGFAULT|Timeout|Subprocess aborted)\)", re.M),  # ctest
    re.compile(r"^FAILED (\S+::\S+)", re.M),                                 # pytest
]
_CACHE: dict = {}


def _enabled() -> bool:
    return os.environ.get("AIFORGE_CHAT_BASE_REDS", "1").strip().lower() not in ("0", "false", "no", "off")


def failing_names(out: str) -> set:
    """Names of the failing tests in a runner's output (empty when none read)."""
    text = out or ""
    names = set()
    for rx in _NAMES:
        names.update(m.group(1).strip() for m in rx.finditer(text))
    return names


def _all_named(out: str, names: set) -> bool:
    """The runner's own failure count is no more than the names read."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _fail_count
        n = _fail_count(out)
    except Exception:  # noqa: BLE001
        return False
    return bool(names) and n != 999 and 0 < n <= len(names)


def _asked_about(st, names: set) -> bool:
    """The request names one of the failing tests: fixing it is the work."""
    goal = str(getattr(st, "goal", "") or "")
    for n in names:
        tail = re.split(r"[.:> ]+", n.strip("() "))[-1]
        if n in goal or (len(tail) > 3 and re.search(rf"\b{re.escape(tail)}\b", goal)):
            return True
    return False


def early_note(st, cwd: str, cmd: str, out: str) -> "str | None":
    """A note for the model when a test fails on two runs of the same test
    command (the command up to its first pipe or redirect) and it fails on
    the original code too. A real Rust run chased an old failure through two
    stuck warnings; its runs differed only in ``| tail -30`` / ``| grep``
    and in which other tests were still red. Once per test name."""
    names = failing_names(out)
    if not names or not hasattr(st, "__dict__"):
        return None
    try:
        from .._shell import test_identity
        key = test_identity(cmd) or cmd
    except Exception:  # noqa: BLE001
        key = cmd
    seen = st.__dict__.setdefault("red_counts", {}).setdefault(key, {})
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    told = st.__dict__.setdefault("base_reds_told", set())
    twice = {n for n in names if seen[n] >= 2 and n not in told}
    if not twice or _asked_about(st, twice):
        return None
    at_base = base_failing(st, cwd, key)
    told.update(twice)                    # asked once, whatever the answer
    old = sorted(twice & (at_base or set()))
    if not old:
        return None
    return (f"These failing tests fail the same way on the original code, before any "
            f"change in this turn: {', '.join(old[:8])}. Your change did not cause them; "
            f"leave them alone unless the request is about them, and judge your work by "
            f"the other tests.")


def base_failing(st, cwd: str, cmd: str, timeout: int = 600) -> "set | None":
    """Failing test names of ``cmd`` run on the commit the work started from
    (a throwaway worktree); None when it cannot be run. Once per command."""
    if not _enabled() or not cmd:
        return None
    try:
        from aiforge_core.runtime import repo_ident
        from ._contract_gate import work_base
        from ._done_watch import _run
        top = repo_ident.git_toplevel(cwd)
        base = work_base(st)
        if not top or not base:
            return None
        rev = subprocess.run(["git", "-C", top, "rev-parse", "--verify", f"{base}^{{commit}}"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    if not rev:
        return None
    key = (top, rev, cmd)
    if key not in _CACHE:
        _CACHE[key] = _base_names(top, rev, cwd, cmd, timeout, _run)
    return _CACHE[key]


def same_reds_at_base(st, cwd: str, cmd: str, out: str, timeout: int = 600) -> bool:
    """Every failing test of ``out`` (``cmd`` run on the turn's code) fails on
    the commit the work started from too. Once per (repo, base, command)."""
    if not _enabled() or not cmd:
        return False
    names = failing_names(out)
    if not _all_named(out, names) or _asked_about(st, names):
        return False
    at_base = base_failing(st, cwd, cmd, timeout)
    return at_base is not None and names <= at_base


def _base_names(top, rev, cwd, cmd, timeout, run) -> "set | None":
    tmp = tempfile.mkdtemp(prefix="af-base-reds-")
    added = False
    try:
        r = subprocess.run(["git", "-C", top, "worktree", "add", "--detach", tmp, rev],
                           capture_output=True, text=True, timeout=60)
        added = r.returncode == 0
        if not added:
            return None
        sub = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
        here = tmp if sub in (".", "") or sub.startswith("..") else os.path.join(tmp, sub)
        line = cmd.replace(os.path.realpath(top), tmp).replace(top, tmp)
        _code, base_out = run(line, here, timeout=timeout)
        from aiforge_core.runtime.failure_signature import _ANSI
        return failing_names(_ANSI.sub("", base_out or ""))
    except Exception:  # noqa: BLE001 — no verdict, never a false green
        return None
    finally:
        if added:
            subprocess.run(["git", "-C", top, "worktree", "remove", "--force", tmp],
                           capture_output=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
