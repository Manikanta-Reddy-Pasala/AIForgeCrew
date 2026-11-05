"""The harness sees the work is done, instead of waiting for the model to say so.

A turn ended only when the model replied without a tool call. On the big
benchmarks it never did: 50 minutes of runs against OpenCode's 17-40, the
board at "5 of 5 still open" while every named part was in the code. The
model kept probing, re-reading and re-testing work that was finished.

The work counts as done, on the code as it is now, when:

* NEW work, named: the request spells out names, signatures or files that
  were MISSING when the turn started (``spec_contract``, a confident index)
  and every one is in the code now. A request that only changes what exists
  never triggers it;
* the full suite is green: a whole-suite run with ``N passed``, where
  failures and errors count against it only if the original code does not
  have them too (the same command on a ``git worktree`` of HEAD) and the
  request does not call them already failing;
* the last test result of the turn is not red, and no target item is open.

Then the model is told the work checks out and to give its final answer.
If it keeps only looking (reads, searches, test runs) for ``GRACE`` more
replies, its next call may not use a tool (``tool_choice=none``): it writes
its answer, which goes through every finish check like any other answer —
nothing is closed or ended behind them. At most ``MAX_FORCED`` times a turn.
After ``IDLE`` replies without an edit, every new name in place, the
harness re-runs the turn's own last full test command once per code state
(the model's runs are often piped or narrowed). A message from the user
stops the watch until the next edit. Off switch:
``AIFORGE_CHAT_DONE_WATCH=0``; grace: ``AIFORGE_CHAT_DONE_GRACE``.
"""
from __future__ import annotations

import os
import re

#: Replies with no edit, every new name present, before the harness runs
#: the tests itself.
IDLE = 8
MAX_FORCED = 2
_RAN = re.compile(r"\b([1-9]\d*) passed\b")
_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)", re.M)
_PROBE_TIMEOUT_S = 900
#: Shell commands that only look: running them is "checking again".
_LOOK = frozenset({"cat", "ls", "grep", "rg", "head", "tail", "wc", "find", "tree",
                   "echo", "pwd", "sed", "awk", "diff", "file", "stat", "which",
                   "python", "python3", "pytest", "tox", "nox", "uv", "pip"})
_GIT_LOOK = frozenset({"status", "diff", "log", "show", "blame", "grep", "ls-files",
                       "rev-parse", "branch"})
_READ_TOOLS = frozenset({"file_read", "read_files", "read_lines", "grep", "find",
                         "list_dir", "repo_map", "git_status", "git_diff", "git_log",
                         "run_tests", "plan_progress", "memory_lookup", "tool_help",
                         "session_actions", "command_output"})


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_DONE_WATCH", "1").strip().lower() not in (
        "0", "false", "no", "off")


def grace() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_CHAT_DONE_GRACE", "6")))
    except ValueError:
        return 6


def _applies(st) -> bool:
    return not any(getattr(st, k, False) for k in (
        "readonly_mode", "plan_mode", "analyze_mode", "strict_finish", "builder"))


def _raw_gaps(st) -> "list | None":
    """What the request names that the code lacks — straight from the
    checker (no steer filter, no cache), or None when the index is not to be
    trusted (truncated, mostly non-Python: only file claims are checked)."""
    from aiforge_core.runtime import spec_contract
    goal = str(getattr(st, "goal", "") or "")
    cwd = getattr(st, "cwd", None)
    if not goal or not cwd:
        return None
    key = ("done_watch", id(st), getattr(st, "edits_made", 0),
           len(getattr(st, "convo", None) or []))
    hit = getattr(st, "dw_gap_cache", None)
    if hit and hit[0] == key:
        return hit[1]
    gaps = spec_contract.missing(goal, str(cwd), key=key)   # builds / reuses the index
    ix = (spec_contract._CACHE.get(str(cwd)) or (None, None))[1]
    if ix is None or ix.truncated or not ix.py \
            or ix.py < spec_contract._MIN_PY_SHARE * max(ix.source, 1):
        gaps = None
    st.dw_gap_cache = (key, gaps)
    return gaps


def _initial_gaps(st) -> int:
    """How many named things were missing when the turn started (read once,
    before the first edit lands)."""
    if getattr(st, "dw_initial", None) is None and getattr(st, "edits_made", 0) == 0:
        try:
            from aiforge_core.runtime import git_target
            where = git_target.command_base(getattr(st, "cwd", None)) or str(st.cwd)
            code, rev = _run("git rev-parse --verify -q HEAD", where, 30)
            st.dw_base_rev = rev.strip() if code == 0 and rev.strip() else None
        except Exception:  # noqa: BLE001
            st.dw_base_rev = None
        try:
            gaps = _raw_gaps(st)
            st.dw_initial = len(gaps) if gaps else 0
        except Exception:  # noqa: BLE001
            st.dw_initial = 0
    return getattr(st, "dw_initial", None) or 0


def _spec_complete(st) -> bool:
    """The named things the turn had to add are all there now."""
    if _initial_gaps(st) <= 0:
        return False
    from ._contract_gate import _waived
    gaps = _raw_gaps(st)
    return gaps is not None and not _waived(st, gaps)


def _targets_open(st) -> bool:
    from ._tasks import open_items
    board = getattr(st, "board", None) or {}
    return any(board[s].get("enforced") for s in open_items(board))


def _green_now(st) -> bool:
    """The full suite passed on the code as it is now."""
    if not getattr(st, "last_green_fp", None):
        return False
    if getattr(st, "edits_made", 0) != getattr(st, "green_at_edits", -1):
        return False
    try:
        from ._outcomes import content_fingerprint
        return content_fingerprint(getattr(st, "cwd", None)) == st.last_green_fp
    except Exception:  # noqa: BLE001
        return False


#: Replies that only re-check after every task-board item is closed, before
#: the final answer is forced (the model itself said each part is done).
BOARD_GRACE = 2


def board_done(st) -> bool:
    """Every item of a real task list (2+ items, at least one closed this turn)
    is done or skipped, the turn's last test run is not red, and no user
    message is waiting: the model has said, item by item, that the work is
    done. Ends the turn sooner than the spec+suite check when the board was
    worked (a long office run kept going after its last item closed)."""
    if not _on() or not _applies(st):
        return False
    board = getattr(st, "board", None) or {}
    if len(board) < 2 or getattr(st, "tests_green", None) is False:
        return False
    if getattr(st, "dw_blocked_at", None) == getattr(st, "edits_made", 0):
        return False
    if not getattr(st, "board_used", False) and not getattr(st, "board_touched", False):
        return False                    # nobody worked this board in this turn
    # done or skipped, not merely closed: a failed item is work not done
    return all(isinstance(it, dict) and it.get("status") in ("done", "skipped")
               for it in board.values())


def done_now(st) -> bool:
    if not _on() or not _applies(st) or getattr(st, "edits_made", 0) <= 0:
        return False
    if getattr(st, "dw_blocked_at", None) == getattr(st, "edits_made", 0):
        return False                    # the user spoke since: wait for their work
    if getattr(st, "tests_green", None) is False:
        return False                    # the last test result of the turn is red
    try:
        return _green_now(st) and _spec_complete(st) and not _targets_open(st)
    except Exception:  # noqa: BLE001 — the watch never ends a turn on its own error
        return False


def _run(cmd: str, cwd: str, timeout: int = _PROBE_TIMEOUT_S, env=None) -> "tuple[int, str]":
    """Run ``cmd`` the way run_command does (``bash -c``, not a login shell),
    in its own process group: on timeout the whole group is killed, not just
    bash."""
    import signal
    import subprocess
    p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace",
                         start_new_session=True, env=env)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return -9, ""


def _interpreter(cmd: str) -> "str | None":
    """The python that ran the turn's tests (``/venv/bin/python -m pytest``)."""
    for w in cmd.split():
        if re.search(r"(?:^|/)python[0-9.]*$", w):
            return w
        if re.search(r"(?:^|/)pytest$", w):
            return w[:-len("pytest")] + "python" if "/" in w else "python"
    return None


_ERRORS = re.compile(r"\b(\d+) errors?\b")


def _errors(out: str) -> int:
    hits = _ERRORS.findall(out or "")
    return int(hits[-1]) if hits else 0


_FAIL_ONLY = re.compile(r"^FAILED\s+(\S+::\S+)", re.M)
_ERROR_ONLY = re.compile(r"^ERROR\s+(\S+::\S+)", re.M)


def pre_existing(cwd: str, cmd: str, out: str, base_rev: "str | None" = None) -> bool:
    """Every failure and error of ``out`` (the turn's own full test command
    run now) is there on the original code too: the same command, run on a
    git worktree of ``base_rev`` (the turn's starting commit; HEAD when
    unknown), the repo's path in the command pointed at the copy and the
    copy first on PYTHONPATH (so an editable install or an absolute path
    cannot import the edited code). FAILED ids must be a subset of the
    original's FAILED, ERROR ids of its ERROR, and the error count no higher."""
    import shutil
    import tempfile
    fails, errs = set(_FAIL_ONLY.findall(out or "")), set(_ERROR_ONLY.findall(out or ""))
    if not fails and not errs and not _errors(out):
        return True
    tmp = tempfile.mkdtemp(prefix="aiforge-base-")
    base = os.path.join(tmp, "base")
    top = ""
    try:
        top = _run("git rev-parse --show-toplevel", cwd, 30)[1].strip()
        rev = base_rev or "HEAD"
        if not top or _run(f"git worktree add --detach {base} {rev}", cwd, 60)[0] != 0:
            return False
        sub = os.path.relpath(os.path.realpath(cwd), os.path.realpath(top))
        here = base if sub == "." else os.path.join(base, sub)
        paths = [here] + ([os.path.join(here, "src")] if os.path.isdir(os.path.join(here, "src"))
                          else [])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]]
                                                      if env.get("PYTHONPATH") else []))
        env.pop("PYTHONSAFEPATH", None)
        line = cmd.replace(top, base)
        line = re.sub(r"(^|\s)PYTHONPATH=\S*", r"\1", line)     # ours wins
        code, base_out = _run(line, here, env=env)
        if not _RAN.search(base_out):
            return False
        return (fails <= set(_FAIL_ONLY.findall(base_out))
                and errs <= set(_ERROR_ONLY.findall(base_out))
                and _errors(out) <= _errors(base_out))
    except Exception:  # noqa: BLE001
        return False
    finally:
        if top:
            _run(f"git worktree remove --force {base}", cwd, 60)
            _run("git worktree prune", cwd, 30)
        shutil.rmtree(tmp, ignore_errors=True)


#: A command the probe may run again: the test command alone (after `cd x &&`
#: and VAR= prefixes) — never one that chains a commit, a deploy or a write.
_CHAINED = re.compile(r"&&|\|\||;|\||>|<|`|\$\(")


def _probe_command(cmd: str) -> "str | None":
    rest = re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd or "")
    return None if _CHAINED.search(rest) else cmd


def _probe_tests(st):
    """Generator: re-run the turn's own last full test command once for this
    code state, when the run has gone ``IDLE`` replies without an edit and
    every new name is in. Green when tests ran (``N passed``) and every
    failure also fails on the original code or the request calls it known."""
    if _green_now(st) or getattr(st, "dw_idle", 0) < IDLE:
        return
    last = getattr(st, "last_full_test", None)
    edits = getattr(st, "edits_made", 0)
    if not last or getattr(st, "dw_probed_at", None) == edits or not _spec_complete(st):
        return
    from aiforge_core.runtime.shell_writes import command_of
    cmd = _probe_command(command_of(last[1]) or "")
    if not cmd:
        return
    st.dw_probed_at = edits
    yield {"type": "thought", "role": "system",
           "text": "⧗ no edits for a while and every new named part is in — re-running "
                   "the turn's full test command to see whether the work is done"}
    verdict = own_test_verdict(st, cmd)
    if verdict is None:
        return
    st.tests_green = verdict[0]
    if verdict[0]:
        from ._outcomes import content_fingerprint
        st.last_green_fp = content_fingerprint(getattr(st, "cwd", None))
        st.green_at_edits = edits


def own_test_verdict(st, cmd: "str | None" = None) -> "tuple[bool, str] | None":
    """The turn's own last full test command (``st.last_full_test``) run now:
    ``(green, output)``, or None when there is none, it is not a bare test
    command, or it ran no tests or failed tests it did not name. Green when
    it passed, or every failure is one the request calls known or one the
    original code has too. The command the model found to work: the
    harness's own guess at the project's tests can fail to even collect
    (click: "(999) failing" on a green suite, two false fix rounds)."""
    if cmd is None:
        last = getattr(st, "last_full_test", None)
        if not last:
            return None
        from aiforge_core.runtime.shell_writes import command_of
        cmd = _probe_command(command_of(last[1]) or "")
        if not cmd:
            return None
    from aiforge_core.runtime import git_target
    from ._outcomes import _only_known_failures
    cwd = git_target.command_base(getattr(st, "cwd", None)) or str(getattr(st, "cwd", "."))
    code, out = _run(cmd, cwd)
    if not _RAN.search(out):
        return None
    n_failed = re.findall(r"\b(\d+) failed\b", out)
    listed = len(set(_FAIL_ONLY.findall(out)))
    if n_failed and int(n_failed[-1]) != listed:
        return None                   # failures it did not name: cannot judge them
    if code != 0 and not listed and not _ERROR_ONLY.search(out):
        return None                   # red for no listed test (coverage floor, tox step)
    green = code == 0 or _only_known_failures(st, {"ok": False, "stdout": out}) \
        or pre_existing(cwd, cmd, out, getattr(st, "dw_base_rev", None))
    return bool(green), out


def _acts(step: dict) -> bool:
    """A tool call that does something beyond looking again."""
    name = str(step.get("name") or "")
    if name in _READ_TOOLS:
        return False
    from aiforge_core.runtime.shell_writes import SHELL_TOOLS, command_of
    if name not in SHELL_TOOLS:
        return True
    cmd = command_of(step.get("args") or {}) or ""
    for part in re.split(r"&&|\|\||;|\||\n", cmd):
        words = part.split()
        while words and ("=" in words[0] or words[0] in ("cd", "sudo", "env", "timeout")):
            words = words[2:] if words[0] in ("cd", "timeout") else words[1:]
        if not words:
            continue
        head = os.path.basename(words[0])
        if head == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in _GIT_LOOK:
                return True
        elif head not in _LOOK:
            return True
        elif head in ("pip", "uv") and any(w in ("install", "uninstall", "add", "remove")
                                           for w in words[1:]):
            return True
    return False


def _replies_only_looked(st) -> bool:
    """Tool calls since the last check only looked again; False when that
    cannot be told (no session log)."""
    sid = getattr(st, "session_id", None)
    if sid is None:
        return False
    try:
        from aiforge_core.runtime import action_log
        steps = [s for s in action_log.live_steps(sid) if isinstance(s, dict)]
    except Exception:  # noqa: BLE001
        return False
    seen = getattr(st, "dw_steps_seen", None)
    st.dw_steps_seen = len(steps)
    if seen is None:
        return True
    return not any(_acts(s) for s in steps[seen:])


def _jobs_running(st) -> bool:
    try:
        from aiforge_core.runtime import cleanup_inventory
        ids = cleanup_inventory.running_job_ids(getattr(st, "session_id", None))
        return ids is None or bool(ids)
    except Exception:  # noqa: BLE001
        return True


def watch(st, n):
    """Generator, once per model call after steering is drained: the note when
    the work is first seen done; a forced answer (no tool) when the model
    keeps only looking. Never ends the turn itself: the answer goes through
    every finish check. Returns None."""
    if not (_on() and _applies(st)):
        return None
    steers = len(getattr(st, "steers", None) or [])
    if getattr(st, "dw_steers", None) is not None and steers != st.dw_steers:
        st.done_noted_at = None              # the user spoke: their words decide,
        st.dw_blocked_at = getattr(st, "edits_made", 0)   # until their work lands
    st.dw_steers = steers
    edits = getattr(st, "edits_made", 0)
    st.dw_idle = 0 if getattr(st, "dw_last_edits", None) != edits else \
        getattr(st, "dw_idle", 0) + 1
    st.dw_last_edits = edits
    try:
        _initial_gaps(st)
        if edits > 0:
            yield from _probe_tests(st)
    except Exception:  # noqa: BLE001 — the probe never breaks a step
        pass
    looked = _replies_only_looked(st)
    spec = done_now(st)
    board = not spec and board_done(st)
    if not (spec or board):
        st.done_noted_at = None              # not (or no longer) done
        return None
    if getattr(st, "done_noted_at", None) is None:
        st.done_noted_at = n
        st.dw_quiet = 0
        from ._convo import _append_directive
        if spec:
            yield {"type": "thought", "role": "system",
                   "text": "✓ the work checks out (full suite green, every new named "
                           "part present) — asking for the final answer"}
            _append_directive(st, (
                "[harness — not the user] Done check: the full test suite passes on "
                "the current code and every new name, signature and file the request "
                "spells out is in the code. Mark the finished task-board items done, "
                "and unless something the request asks for is still missing (then "
                "name it and do it), stop now: reply with your final summary and no "
                "tool call."))
        else:
            yield {"type": "thought", "role": "system",
                   "text": "✓ every task-board item is done — asking for the summary"}
            _append_directive(st, (
                "[harness — not the user] Every item on the task board is done. "
                "Unless something the request asks for is still missing (then add it "
                "to the board and do it), stop now: reply with your final summary of "
                "what was done and what was checked, and no tool call."))
        return None
    st.dw_quiet = (getattr(st, "dw_quiet", 0) + 1) if looked else 0
    if (st.dw_quiet < (grace() if spec else BOARD_GRACE) or _jobs_running(st)
            or getattr(st, "dw_forced", 0) >= MAX_FORCED):
        return None
    st.dw_forced = getattr(st, "dw_forced", 0) + 1
    st.dw_quiet = 0
    from .._native import force_final_next
    force_final_next(getattr(st, "session_id", None))
    yield {"type": "thought", "role": "system",
           "text": f"✓ {grace() if spec else BOARD_GRACE} replies after the done "
                   "check, only re-checking — the "
                   "next step writes the final answer"}
    from ._convo import _append_directive
    _append_directive(st, (
        "[harness — not the user] The work checked out and the last replies only "
        "re-checked it. Write your final answer now (no tool call this step)."))
    return None


__all__ = ["BOARD_GRACE", "IDLE", "MAX_FORCED", "board_done", "done_now", "grace",
           "pre_existing", "watch"]
