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
#: After this many landed edits, a *different* test command without a new
#: edit no longer clears the shell streak (late-game plateau: sqlglot g2a
#: stuck at 15/25 for 30 minutes). Earlier, varied pytest is normal research
#: for Tokens and must still reset (b1a/boc* forced bad early edits).
LATE_EDITS = 10
#: Reads in a row (no landed edit, no test run between) before each note:
#: a recorded sqlglot run read 52 times in a row; replayed 12 reads in, a
#: "run the tests now" message sent the model to the tests 8 of 8 times.
READS_EVERY = 12
_READS = frozenset({"read_lines", "file_read", "read_files", "grep", "find", "list_dir"})
#: Waiting on or watching long-running work: never probing. A turn that runs a
#: long job (a suite, a build, a deploy) polls it with these; counting them
#: pushed it to "write the change" in the middle of the wait.
_WAITING = frozenset({"watch_until", "serve", "command_wait", "command_output",
                      "command_kill"})
#: Reading a log or a job's output file is watching a job, not reading code.
_LOG_NAME = re.compile(r"\.(?:log|out|err|jsonl)$", re.I)


def _outside(path: str, cwd) -> bool:
    """``path`` lies outside the turn's workspace (a job's output elsewhere)."""
    import os.path
    if not cwd or not os.path.isabs(path):
        return False
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(str(cwd))]) \
            != os.path.realpath(str(cwd))
    except ValueError:
        return True


def _watching(st, name, args) -> bool:
    """The call waits on, polls or watches running work: a job tool, a
    command started in the background (not a probe), or a read of a log /
    output file. A ``.txt`` or a /tmp file counts only outside the workspace:
    scheduled jobs work in a /tmp folder, and requirements.txt is code."""
    if name in _WAITING:
        return True
    try:
        from ._waiting import shell_poll
        if shell_poll(st, name, args):
            return True
    except Exception:  # noqa: BLE001
        pass
    a = args if isinstance(args, dict) else {}
    if name not in _READS:
        try:
            from aiforge_core.runtime.shell_writes import command_of
            cmd = (command_of(a) or "").rstrip()
        except Exception:  # noqa: BLE001
            cmd = str(a.get("cmd") or "").rstrip()
        backgrounded = bool(a.get("background")) or (cmd.endswith("&") and not cmd.endswith("&&"))
        return backgrounded and not _is_probe(a)
    cwd = getattr(st, "cwd", None)
    for p in [a.get("path"), a.get("file"), *(a.get("paths") or [])]:
        if not isinstance(p, str):
            continue
        if _LOG_NAME.search(p):
            return True
        if _outside(p, cwd) and (p.lower().endswith(".txt") or "/tmp/" in p or "/logs/" in p):
            return True
    return False


def _force_on() -> bool:
    return os.environ.get("AIFORGE_CHAT_FORCE_WRITE", "1").strip().lower() not in (
        "0", "false", "no", "off")
_PROBE = re.compile(r"\bpython[0-9.]*\s+(?:-c\b|-\s*<<|<<)")


def _shell_tools() -> frozenset:
    from aiforge_core.runtime.shell_writes import SHELL_TOOLS
    return SHELL_TOOLS


_SHELL_READS = frozenset({
    "awk", "gawk", "sed", "head", "cat", "nl",
    "grep", "egrep", "fgrep",
})


def is_shell_read(args, name: str | None = None) -> bool:
    """A shell command that only prints a file (awk, sed -n, grep, head).

    sqlglot qw1 re-read parser.py through awk a hundred times. Those calls
    are not read_lines, so the duplicate-read guard never saw them. Looking at
    a job (tail, a log, a /tmp folder, a watch tool) is never one: the file
    grows while the job runs.
    """
    try:
        import shlex

        if name is not None and (name not in _shell_tools() or name in _WAITING):
            return False
        from aiforge_core.runtime.shell_writes import command_of
        cmd = command_of(args) or ""
        if re.match(r"^\s*cd\s+(?:/tmp/|/var/)", cmd):
            return False                # a job's folder, not the code
        cmd = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd)
        bare = re.sub(r"'[^']*'|" + r'"[^"]*"', "", cmd)
        if ">>" in bare or ">" in bare:
            return False
        if re.search(r"\bawk\b", cmd) and re.search(
                r"\bprintf?\b[^;}]*>|\bsystem\s*\(", cmd):
            return False                # awk writing a file from inside its script
        # `tail x.txt; sleep 30` -> separators as words of their own
        spaced = re.sub(r"\s*(\|\||&&|;|\|)\s*", r" \1 ", bare)
        words = [w for w in shlex.split(spaced) if w]
        progs = []
        take = True
        for w in words:
            if w in ("&&", ";", "||", "|"):
                take = True
                continue
            if take:
                progs.append(os.path.basename(w))
                take = False
        if not progs or "tail" in progs:
            return False
        if "sed" in progs and any(w.startswith(("-i", "--in-place")) for w in words):
            return False
        if not all(prog in _SHELL_READS for prog in progs):
            return False
        return not any(_LOG_NAME.search(w) or w.startswith(("/tmp/", "/var/log/"))
                       for w in shlex.split(cmd))
    except Exception:  # noqa: BLE001
        return False



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
    try:
        from ._waiting import note_background
        note_background(st, name, args)
    except Exception:  # noqa: BLE001
        pass
    edits = getattr(st, "edits_made", 0)
    if edits != getattr(st, "probe_edits_seen", 0):
        # an edit LANDED since the last step (a refused one does not count)
        st.probe_edits_seen = edits
        st.shell_streak = 0
        st.read_streak = 0
    if _is_test(name, args):
        # The same test on the same code is not progress. sqlglot qw1 ran
        # awk, then pytest, then the same awk: every pytest zeroed the
        # streak, so 200 shell calls never reached the forced edit.
        # Early (few edits): a different pytest is research for Tokens —
        # reset. Late (LATE_EDITS+): varied pytest without a new edit is the
        # g2a plateau — keep/count the streak so FORCE_AT can fire (boc1a
        # sat at 3/25 on pytest+reads only).
        cmd = " ".join(str((args or {}).get("cmd") or (args or {}).get("command") or "").split())
        same = (cmd and cmd == getattr(st, "probe_last_test", None)
                and getattr(st, "edits_made", 0) == getattr(st, "probe_test_edits", -1))
        late = edits >= LATE_EDITS
        if not same:
            st.probe_last_test = cmd
            st.probe_test_edits = getattr(st, "edits_made", 0)
            st.read_streak = 0
            if not late:
                st.shell_streak = 0
                return ""
            if edits != getattr(st, "probe_shell_reset_edits", None):
                st.shell_streak = 0
                st.probe_shell_reset_edits = edits
                return ""
        elif not late:
            return ""
        return _shell_streak_note(st, edits, what="test runs")
    if _watching(st, name, args):
        return ""                      # waiting on a long job: not probing, no count
    if name in _READS:
        st.read_streak = getattr(st, "read_streak", 0) + 1
        if edits and st.read_streak % READS_EVERY == 0:
            # sqlglot acc1: 117 reads between edits. The note said "run the
            # tests", the tests were already red, and the model read again.
            # Once a test has failed, the next step can only edit.
            if (getattr(st, "tests_green", None) is False and _force_on()
                    and getattr(st, "native_on", False)):
                st.want_force_write = True
                st.read_streak = 0
                return ("[harness — not the user] The tests already failed and "
                        "these reads are not changing the code. Your next step "
                        "can only edit files: change the file the failure names "
                        "(file_patch / multi_edit). Do not read again and do not "
                        "re-run the tests until that edit is saved.")
            # boc8a: Tokens/exprs landed, then 45 reads + shell diagnosing
            # missing _parse_trigger — notes said "run tests", force never
            # armed (tests never marked red), score stuck 7/25 until SIGTERM.
            if (st.read_streak >= READS_EVERY * 2 and _force_on()
                    and getattr(st, "native_on", False)):
                st.want_force_write = True
                st.read_streak = 0
                return ("[harness — not the user] "
                        f"{READS_EVERY * 2}+ reads since the last edit and the "
                        "request still has open parts. Your next step can only "
                        "edit files (file_patch / multi_edit / file_write) for "
                        "the running board item — implement the missing code "
                        "now; do not read again first.")
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
    return _shell_streak_note(st, edits, what="shell commands")


def _shell_streak_note(st, edits: int, *, what: str) -> str:
    """Count one no-edit step toward FORCE_AT; note or arm a forced write."""
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
        return (f"[harness — not the user] {FORCE_AT}+ {what} and no change "
                "to the files after two notes. Your next step can only edit files: "
                "write the change you have been investigating now (file_patch / "
                "multi_edit), then run the tests.")
    if what == "test runs":
        return (f"[harness — not the user] {st.shell_streak} test runs in a row "
                "and no change to the files. The suite is not moving: edit the "
                "code the failures name (file_patch / multi_edit), then re-run.")
    return (f"[harness — not the user] {st.shell_streak} shell commands in a row "
            "and no change to the files. If the task needs a code change, stop "
            "probing with throwaway scripts and cat/grep: read what you need with "
            "the read tools, write the change (file_write / multi_edit), then run "
            "the repo's tests.")


__all__ = ["EVERY", "LATE_EDITS", "FORCE_AT", "note"]
