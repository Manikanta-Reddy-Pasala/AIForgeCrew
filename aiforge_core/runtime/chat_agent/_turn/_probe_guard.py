"""Shell commands in a row with no change to the code: a model probing
instead of working.

On a 5-feature benchmark the agent ran 101 shell commands in a row (and
streaks of 37 and 32) without one edit: throwaway `python3 -c "..."` scripts
trying the code, cat/grep through the shell instead of the read tools. The
same model under OpenCode read, wrote large edits and ran the tests. Every
``EVERY`` commands without a landed edit the observation says so. Not in a
read-only turn (it may not write), and worded so an ops turn that only runs
commands is not pushed to write code.
Off switch: ``AIFORGE_CHAT_PROBE_GUARD=0``.
"""
from __future__ import annotations

import os
import re

#: Shell commands in a row (no landed edit between) before each note.
EVERY = 10
#: Throwaway python probes in the whole turn (edits between them or not)
#: before each note: a later run made 56 `python -c "...hasattr(...)"` checks
#: between edits, which the in-a-row count never saw.
PROBES_EVERY = 8
#: Shell commands in a row (two notes ignored) before the next call may only
#: edit files. Off switch: ``AIFORGE_CHAT_FORCE_WRITE=0``. (Turned off once:
#: the next click run made 281 commands and 253 reads, 615 calls, against
#: 196 and 85 with it on — the step is what ends a probing streak.)
FORCE_AT = 20
#: Reads in a row (no landed edit, no test run between) before each note:
#: a recorded sqlglot run read 52 times in a row; replayed 12 reads in, a
#: "run the tests now" message sent the model to the tests 8 of 8 times.
READS_EVERY = 12
_READS = frozenset({"read_lines", "file_read", "read_files", "grep", "find", "list_dir"})


def _force_on() -> bool:
    return os.environ.get("AIFORGE_CHAT_FORCE_WRITE", "1").strip().lower() not in (
        "0", "false", "no", "off")
_PROBE = re.compile(r"\bpython[0-9.]*\s+(?:-c\b|-\s*<<|<<)")


def _shell_tools() -> frozenset:
    from aiforge_core.runtime.shell_writes import SHELL_TOOLS
    return SHELL_TOOLS


def _writes_through_shell(args, cwd) -> bool:
    try:
        from aiforge_core.runtime.shell_writes import command_of, shell_write_targets
        return bool(shell_write_targets(command_of(args) or "", str(cwd or ".")))
    except Exception:  # noqa: BLE001
        return False


def _is_probe(args) -> bool:
    try:
        from aiforge_core.runtime.shell_writes import command_of
        cmd = command_of(args) or ""
    except Exception:  # noqa: BLE001
        cmd = str((args or {}).get("cmd") or "")
    return bool(_PROBE.search(cmd)) and "pytest" not in cmd


def _test_cmd(st) -> str:
    """The turn's own last test command, to name in a note ("" if none)."""
    last = getattr(st, "last_test_cmd", None)
    if not last:
        return ""
    if last[0] == "run_tests":
        return "run_tests"
    try:
        from aiforge_core.runtime.shell_writes import command_of
        return (command_of(last[1]) or "").strip()[:300]
    except Exception:  # noqa: BLE001
        return ""


def _run_tests_now(st) -> str:
    cmd = _test_cmd(st)
    return (f"run the tests now ({cmd})" if cmd else
            "run the tests that cover your change now") + " and fix the first failure they show"


_RUNNERS = frozenset({"pytest", "py.test", "tox", "nox", "npm", "yarn", "pnpm", "jest",
                      "vitest", "mocha", "mvn", "gradle", "gradlew", "go", "cargo",
                      "dotnet", "make", "ctest", "tsc", "eslint", "ruff", "mypy", "pyright"})
_PY_MODULES = frozenset({"pytest", "unittest", "tox", "nox", "mypy", "ruff"})


def _is_test(name, args) -> bool:
    """A test, build or check run: the shell's own definition, and the
    program that runs is a test runner (``grep -rn pytest .`` names pytest
    but runs grep)."""
    try:
        import os.path
        import shlex
        from .._shell import is_test_command
        if not is_test_command(name, args):
            return False
        if name in ("run_tests", "typecheck"):
            return True
        cmd = str((args or {}).get("cmd") or (args or {}).get("command") or "")
        cmd = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)
        words = [w for w in shlex.split(cmd) if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if not words:
            return False
        prog = os.path.basename(words[0])
        if prog.startswith("python"):
            return len(words) > 2 and words[1] == "-m" and words[2] in _PY_MODULES
        return prog in _RUNNERS
    except Exception:  # noqa: BLE001 — unsure: not a test run
        return False


def note(st, name: str, args) -> str:
    """The line to add to this observation, or ""."""
    if os.environ.get("AIFORGE_CHAT_PROBE_GUARD", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    if any(getattr(st, k, False) for k in ("readonly_mode", "plan_mode", "analyze_mode")):
        return ""
    edits = getattr(st, "edits_made", 0)
    if edits != getattr(st, "probe_edits_seen", 0):
        # an edit LANDED since the last step (a refused one does not count)
        st.probe_edits_seen = edits
        st.shell_streak = 0
        st.read_streak = 0
    if _is_test(name, args):
        st.read_streak = 0             # a test run is progress, whatever tool ran it
        st.shell_streak = 0
        return ""
    if name in _READS:
        st.read_streak = getattr(st, "read_streak", 0) + 1
        if edits and st.read_streak % READS_EVERY == 0:
            return (f"[harness — not the user] {st.read_streak} reads in a row and no "
                    f"edit or test run. Stop reading for now: {_run_tests_now(st)}; read "
                    "only what that failure points at.")
        return ""
    if name not in _shell_tools():
        return ""
    if _writes_through_shell(args, getattr(st, "cwd", None)):
        st.shell_streak = 0          # sed -i / cat > f: a change, made the shell way
        return ""
    if _is_probe(args):
        st.probe_count = getattr(st, "probe_count", 0) + 1
        if st.probe_count % PROBES_EVERY == 0:
            return (f"[harness — not the user] {st.probe_count} throwaway python "
                    f"scripts this turn to check the code. Stop probing: {_run_tests_now(st)}. "
                    "To see whether a name exists, use grep or read the file.")
    st.shell_streak = getattr(st, "shell_streak", 0) + 1
    if st.shell_streak % EVERY:
        return ""
    if st.shell_streak >= FORCE_AT and edits and _force_on() \
            and getattr(st, "native_on", False):
        # Two notes ignored, in a run that writes code: the next call can only
        # edit (click af6 ran 135 shell commands, 25 minutes, no edit). The
        # call site arms it once this note is really in the observation.
        st.want_force_write = True
        st.shell_streak = 0
        return (f"[harness — not the user] {FORCE_AT}+ shell commands and no change "
                "to the files after two notes. Your next step can only edit files: "
                "write the change you have been investigating now (file_patch / "
                "multi_edit), then run the tests.")
    return (f"[harness — not the user] {st.shell_streak} shell commands in a row "
            "and no change to the files. If the task needs a code change, stop "
            "probing with throwaway scripts and cat/grep: read what you need with "
            "the read tools, write the change (file_write / multi_edit), then run "
            "the repo's tests.")


__all__ = ["EVERY", "note"]
