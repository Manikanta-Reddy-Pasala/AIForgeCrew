"""What a test or build run proved: a green suite on this exact tree, or the
same failure again.

Two readers. The verify-on-final gate skips its own run when the agent
already ran the full suite green on the tree as it is now — "as it is now"
means file CONTENT, not the list of dirty files, and "green" means the test
runner said so, not that a pipe to ``tail`` exited 0. And the same-failure
loop rule (:mod:`aiforge_core.runtime.same_failure`) sees every failing
command result, so a run that edits something different each round while
the same test keeps failing is caught.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess

from aiforge_core.runtime import cmd_finished, same_failure
from aiforge_core.runtime.failure_signature import (
    failure_of,
    result_text,
    runner_green,
)

from .._context import (
    _run_project_verify,
    _verify_fix_message,
    _verify_max_rounds,
    _verify_on_final_enabled,
    lint_only,
)
from . import _gaming
from ._progress import _SHELL_TOOLS, _digest, _refresh_tree

_TEST_CMD = re.compile(
    r"\b(pytest|npm test|npm run test|yarn test|pnpm test|jest|vitest|"
    r"mvn\S* (?:\S+ )*?(?:test|verify)|go test|cargo test|gradlew? (?:\S+ )*?test|"
    r"python -m unittest|tox)\b")
#: A narrowed run proves only the part it ran.
_NARROW = re.compile(
    r"\s-k\b|\s-m\s+(?!pytest\b|unittest\b|tox\b)|--lf\b|--last-failed\b|--deselect\b|\s-t\b|"
    r"\s-x\b|--exitfirst\b|--maxfail\b|"
    r"--testNamePattern\b|--tests\b|\s-run\b|-Dtest=|-Dit\.test=|::|"
    r"\s[\w./-]+\.(?:py|js|jsx|ts|tsx|java|kt|go|rs)\b")
_FULL_MODES = ("all", "full")
_UNTRACKED_MAX = 500


def _is_test_run(name, args) -> bool:
    if name == "run_tests":
        return True
    cmd = str((args or {}).get("cmd") or (args or {}).get("command") or "")
    return name in _SHELL_TOOLS and bool(_TEST_CMD.search(cmd))


def _full_and_honest(name, args) -> bool:
    """The run covered the whole suite and its exit code is the runner's:
    no pipe (``| tail`` exits 0 whatever pytest did), no ``;`` or
    ``|| true``, no ``-k``/single-file narrowing."""
    args = args or {}
    if name == "run_tests":
        return (not str(args.get("pattern") or "").strip()
                and str(args.get("mode") or "fast") in _FULL_MODES)
    cmd = " " + str(args.get("cmd") or args.get("command") or "")
    return "|" not in cmd and ";" not in cmd and not _NARROW.search(cmd)


def _git_out(cwd, *args) -> bytes | None:
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", *args], cwd=str(cwd),
            capture_output=True, timeout=10,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return None
    return out.stdout if out.returncode == 0 else None


def content_fingerprint(cwd) -> str | None:
    """A hash of the tree's CONTENT against HEAD: the diff of tracked files
    plus every untracked file's bytes. None outside a git repository (no
    signal — never "unchanged")."""
    if not cwd:
        return None
    diff = _git_out(cwd, "diff", "HEAD", "--no-ext-diff", "--binary")
    if diff is None:
        return None
    digest = hashlib.sha1(diff)  # noqa: S324 — not security
    listing = _git_out(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    if listing is None:
        return None
    for rel in sorted(p for p in listing.split(b"\0") if p)[:_UNTRACKED_MAX]:
        path = rel.decode("utf-8", "replace")
        digest.update(rel + b"\0" + _digest(os.path.join(str(cwd), path)).encode())
    return digest.hexdigest()


def _note_green_tests(st, name, args, result, cwd) -> None:
    """Remember a passing FULL test run and the content it ran against. A
    command checked on until it finished counts as its original command; one
    still running proves nothing yet."""
    from aiforge_core.runtime.cleanup_detect import JOB_TOOLS
    starts = st.__dict__.setdefault("job_start_edits", {}) if hasattr(st, "__dict__") else {}
    if name not in JOB_TOOLS and isinstance(result, dict) and result.get("id") \
            and (result.get("running") or result.get("background")):
        starts[str(result["id"])] = getattr(st, "edits_made", 0)   # handed off
    job = str((args or {}).get("id") or "") if name in JOB_TOOLS else ""
    # a job checked on after edits landed tested the code as it was then
    stale = bool(job) and starts.get(job, getattr(st, "edits_made", 0)) \
        != getattr(st, "edits_made", 0)
    run = cmd_finished.as_run(name, args, result)
    if run is None:
        return
    name, args, result = run
    if not isinstance(result, dict) or not _is_test_run(name, args):
        return
    if stale:
        return                        # tested the code as it was before the edits
    st.last_test_cmd = (name, dict(args or {}))      # any test run: its interpreter
    known = _only_known_failures(st, result)
    _gaming.note_test_result(st, True if known else result.get("ok"))
    if result.get("ok") is not True and not known:
        st.last_green_fp = None
        if _full_and_honest(name, args):
            st.last_full_test = (name, dict(args or {}))
            st.last_full_green = False
        return
    if not _full_and_honest(name, args):
        return                        # proves nothing about the whole suite
    st.last_full_test = (name, dict(args or {}))
    # a narrow green run after a red full one leaves the suite red: the
    # board-done check reads this, not tests_green (click t5 closed on a
    # green single-file run 4 s after the full suite failed)
    st.last_full_green = True
    text = result_text(result)
    if not (known or (runner_green(text) and _RAN.search(text))):
        return                        # a build with no test count ran no tests
    try:
        st.last_green_fp = content_fingerprint(cwd)
        st.green_at_edits = getattr(st, "edits_made", 0)
    except Exception:  # noqa: BLE001
        st.last_green_fp = None


_RAN = re.compile(r"\b[1-9]\d* passed\b")


_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+)", re.M)


_KNOWN_WORDS = re.compile(r"already fail|known (?:failure|to fail|issue)|ignore it|"
                          r"flaky|pre-?existing|expected to fail|xfail", re.I)
#: The sentence asks for the test to be fixed: it is not one to ignore.
_FIX_WORDS = re.compile(r"\b(?:fix|repair|make (?:it|them|\S+) pass|should pass|must pass|"
                        r"don'?t ignore|do not ignore|not (?:a )?(?:flaky|known)|isn'?t flaky|"
                        r"regression|investigate|remove the xfail|un-?xfail)\b", re.I)


def _said_known(goal: str, name: str) -> bool:
    """The request names ``name`` (as a whole word) as already failing — not
    "fix it so `test_x` passes"."""
    for m in re.finditer(rf"(?<![\w]){re.escape(name)}(?![\w])", goal):
        # the sentence that names it, not the one before or after
        start = max(goal.rfind(". ", 0, m.start()), goal.rfind("\n\n", 0, m.start()))
        ends = [i for i in (goal.find(". ", m.end()), goal.find("\n\n", m.end())) if i >= 0]
        near = goal[start + 1 if start >= 0 else 0:min(ends) if ends else len(goal)]
        if _KNOWN_WORDS.search(near) and not _FIX_WORDS.search(near):
            return True
    return False


def _only_known_failures(st, result) -> bool:
    """Every failing test is one the request itself says already fails
    ("`test_lazy_load` already fails in this environment; ignore it"): the
    suite is as green as it can be. Needs the failing ids printed."""
    try:
        text = result_text(result)
        ids = _FAILED_ID.findall(text)
        goal = str(getattr(st, "goal", "") or "")
        if not ids or not goal or not re.search(r"\b\d+ failed\b", text) \
                or re.search(r"\b\d+ errors?\b", text):
            return False
        n_failed = int(re.findall(r"\b(\d+) failed\b", text)[-1])
        if n_failed != len(set(ids)):
            return False
        return all(_said_known(goal, i.rsplit("::", 1)[-1]) for i in ids)
    except Exception:  # noqa: BLE001
        return False


# ── the same failure, fix after fix ──────────────────────────────────────

def _state_key(st) -> str:
    if getattr(st, "tree_pending", False):
        with contextlib.suppress(OSError, ValueError, TypeError):
            _refresh_tree(st)         # a shell edit is a new state too
    return str(getattr(st, "state_fp", "") or "")


def note_failure(st, name, args, result):
    """Feed a failing test/build/command result to the same-failure rule.
    Returns None, ``("nudge", text)`` or ``("stop", text)``. A command
    checked on until it finished is judged on its whole output, as its
    original command; partial output of one still running never is."""
    run = cmd_finished.as_run(name, args, result)
    if run is None:
        return None
    name, args, result = run
    if name != "run_tests" and name not in _SHELL_TOOLS:
        return None
    if not isinstance(result, dict) or result.get("ok") is not False:
        return None
    if result.get("timed_out") or result.get("stopped") or result.get("blocked"):
        return None                   # not the code's failure
    fail = failure_of(result_text(result))
    return _judge(st, fail)


def _judge(st, fail):
    track = getattr(st, "same_fail", None)
    if track is None or not fail.signature:
        return None
    return same_failure.judge(track, fail, _state_key(st))


# ── verify on FINAL ──────────────────────────────────────────────────────

def _verify_on_final(st, step, cwd, plan_mode, builder):
    """Verify->fix on FINAL, then — tests green — the test-gaming check
    (:mod:`._gaming`). Returns continue or None."""
    sig = yield from _verify_fix(st, step, cwd, plan_mode, builder)
    if sig == "continue":
        return sig
    return (yield from _gaming.gate(st, step, cwd, plan_mode, builder))


def _verify_fix(st, step, cwd, plan_mode, builder):
    """Progress-gated verify->fix on FINAL: only an act-mode run that edited
    files with a real suite; loop while failures drop, else accept honestly.
    Returns continue or None."""
    # A + B: enforced verify→fix on FINAL (progress-gated). Only for an
    # act-mode run that actually EDITED files with a real test suite —
    # a Q&A turn (0 edits) or read-only plan mode is untouched. Up to
    # _verify_max_rounds() fix rounds (1 by default) while the failure count
    # drops; then the HONEST still-failing final is accepted rather than
    # churn. Opt out: AIFORGE_CHAT_VERIFY_ON_FINAL=0.
    # The agent already ran the whole suite green and the tree's content
    # has not changed since: running it again is the triple test run.
    if (getattr(st, "last_green_fp", None)
            and content_fingerprint(cwd) == st.last_green_fp):
        return None
    if plan_mode or builder or st.edits_made <= 0 or not _verify_on_final_enabled():
        return None
    if st.verify_rounds >= _verify_max_rounds():
        # Out of fix rounds: no more send-backs, but the answer still says
        # what the checks show (a run improving 9 → 5 → 2 used all its rounds
        # and then answered "all tests pass" with 2 red).
        # once per code state: a later answer after more edits (a gaming
        # send-back, say) is checked again; the same tree never twice
        here = content_fingerprint(cwd) or f"edits:{st.edits_made}"
        if getattr(st, "verify_capped_at", None) == here:
            return None
        st.verify_capped_at = here
        _vok, _vout = _verify_known_ok(st, *_project_verify(st, cwd))
        _gaming.note_test_result(st, _vok)
        if _vok is not None:
            st.last_full_green = _vok     # a whole-tree verdict
        if _vok is False:
            try:
                from aiforge_core.runtime.parallel_subtasks import _fail_count
                _fails = _fail_count(_vout)
            except Exception:  # noqa: BLE001
                _fails = 1
            yield {"type": "thought", "role": "system",
                   "text": f"⚠ tests still failing ({_fails}) after "
                           f"{st.verify_rounds} fix rounds — stopping with the honest state."}
            step["text"] = still_failing_note(_fails, _vout) + (step.get("text") or "")
        return None
    yield {"type": "thought", "role": "system",
           "text": "⧗ running the project's checks before finishing…"}
    _vok, _vout = _verify_known_ok(st, *_project_verify(st, cwd))
    _gaming.note_test_result(st, _vok)
    if _vok is not None:
        st.last_full_green = _vok         # a whole-tree verdict
    if _vok is not False:
        return None
    try:
        from aiforge_core.runtime.parallel_subtasks import _fail_count
        _fails = _fail_count(_vout)
    except Exception:  # noqa: BLE001
        _fails = 1
    if st.verify_prev_fails is not None and _fails >= st.verify_prev_fails:
        st.verify_stalls += 1
    else:
        st.verify_stalls = 0
    st.verify_prev_fails = _fails
    seen = _judge(st, failure_of(_vout))
    if st.verify_stalls < 2 and not (seen and seen[0] == same_failure.STOP):
        st.verify_rounds += 1
        if step.get("text"):        # the streamed answer, set aside
            yield {"type": "thought", "text": step["text"]}
        what = ("tests pass, lint found new undefined names" if lint_only(_vout)
                else f"tests failing ({_fails})")
        yield {"type": "thought", "role": "system",
               "text": f"✗ {what} — fixing "
                       f"(verify round {st.verify_rounds}/"
                       f"{_verify_max_rounds()})…"}
        msg = _verify_fix_message(_vout)
        if seen:
            msg += "\n\n" + seen[1]
        st.convo.append({"role": "user", "content": msg})
        return "continue"
    what = ("lint still finds new undefined names" if lint_only(_vout)
            else f"tests still failing ({_fails})")
    yield {"type": "thought", "role": "system",
           "text": f"⚠ {what} after "
                   f"{st.verify_rounds} fix rounds — stopping with "
                   "the honest state."}
    # The state is only honest if the ANSWER says so. Live: the run stopped
    # here with one test failing and the answer sent was "Done. pytest →
    # 1 passed". The harness ran the checks itself, so it says what it saw.
    step["text"] = still_failing_note(_fails, _vout) + (step.get("text") or "")
    return None


def _project_verify(st, cwd):
    """``(ok, output)`` of the finish check: the harness's own project runner,
    unless it could not run the tests at all (no counts, a collection error)
    while the turn has a full test command that does (``own_test_verdict``).
    click: the runner's guessed command failed to collect a suite the model
    ran green ("(999) failing", two false fix rounds). The runner's verdict
    stands whenever it ran tests: it covers the whole tree and the lint legs."""
    ok, out = _run_project_verify(cwd)
    try:
        from aiforge_core.runtime.parallel_subtasks import _fail_count
        if ok is not False or lint_only(out) or _fail_count(out) != 999:
            return ok, out
        from aiforge_core.runtime.shell_writes import command_of
        from ._done_watch import _interpreter, own_test_verdict
        last = getattr(st, "last_full_test", None)
        cmd = command_of(last[1]) if last else ""
        from aiforge_core.runtime import git_target
        base = git_target.command_base(getattr(st, "cwd", None) or cwd) or cwd
        if not _whole_suite(cmd or "", base):
            # a narrower run cannot stand for the tree; a runner that cannot
            # run this suite on the original code either has no verdict
            return (None, out) if _runner_broken_at_base(st, cwd) else (ok, out)
        own = own_test_verdict(st)
    except Exception:  # noqa: BLE001 — the runner's verdict stands
        return ok, out
    if own is None:
        return (None, out) if _runner_broken_at_base(st, cwd) else (ok, out)
    green, own_out = own
    if not green:
        return False, own_out
    try:
        from aiforge_core.runtime.integration_report import _static_lint_python
        py = _interpreter(cmd)
        if py:
            lint_ok, lint = _static_lint_python(cwd, py, None)
            if not lint_ok:
                return False, own_out + lint
    except Exception:  # noqa: BLE001 — the lint leg is best effort
        pass
    # (the python runner it stands in for runs this lint leg only, no
    # tsc / go vet gate)
    return True, own_out


_BROKEN_AT_BASE: dict = {}


def _runner_broken_at_base(st, cwd) -> bool:
    """The harness's project runner cannot run this repo's tests on the
    original code either (no counts, a collection error there too): its
    'could not run' says nothing about the turn's edits, so the finish has no
    verdict instead of a false "checks still FAIL" (sqlglot: a pytest
    deprecation warning breaks the runner's collection; a 6/6 feature answer
    went out with that note). Checked once per repo and base commit on a
    throwaway git worktree that reuses the repo's runner venv (no install:
    an install failure must not read as a broken base); a broken conftest the
    turn wrote still fails. No check without the turn's recorded base commit
    or an existing runner venv."""
    import shutil
    import subprocess
    import tempfile
    from aiforge_core.runtime import repo_ident
    from aiforge_core.runtime._report_pytest import _AIFORGE_VENV
    top = repo_ident.git_toplevel(cwd)
    base = getattr(st, "dw_base_rev", None)
    venv = os.path.join(top, _AIFORGE_VENV) if top else ""
    if not top or not base or not os.path.isdir(venv):
        return False
    try:
        rev = subprocess.run(["git", "-C", top, "rev-parse", "--verify", f"{base}^{{commit}}"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not rev:
        return False
    key = (top, rev)
    if key in _BROKEN_AT_BASE:
        return _BROKEN_AT_BASE[key]
    broken = False
    tmp = tempfile.mkdtemp(prefix="af-runner-base-")
    added = False
    try:
        r = subprocess.run(["git", "-C", top, "worktree", "add", "--detach", tmp, rev],
                           capture_output=True, text=True, timeout=60)
        added = r.returncode == 0
        if added:
            os.symlink(venv, os.path.join(tmp, _AIFORGE_VENV))
            from aiforge_core.runtime.parallel_subtasks import _fail_count
            ok, out = _run_project_verify(tmp)
            broken = ok is False and _fail_count(out) == 999 and not lint_only(out)
    except Exception:  # noqa: BLE001 — unknown: keep the runner's verdict
        broken = False
    finally:
        if added:
            subprocess.run(["git", "-C", top, "worktree", "remove", "--force", tmp],
                           capture_output=True, text=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "-C", top, "worktree", "prune"],
                       capture_output=True, text=True, timeout=30)
    _BROKEN_AT_BASE[key] = broken
    return broken


_TOP_TESTS = frozenset({"tests", "test", "testing", "."})
#: pytest options that take a value as the next word.
_VALUE_OPTS = frozenset({"-p", "--rootdir", "--basetemp", "-W", "--junitxml",
                         "--log-level", "--tb", "-r", "--durations"})
#: options that narrow what is collected or skip conftests / passed tests
_NOT_WHOLE = ("--ignore", "--deselect", "--noconftest", "--sw", "--stepwise",
              "--confcutdir", "-c", "--config-file", "--lf", "--last-failed",
              "--ff", "--pyargs")


def _whole_suite(cmd: str, base: str = "") -> bool:
    """A pytest run over the whole suite: no paths after ``pytest``, or only
    the top-level test folder (``tests``), from the folder the shell starts
    in (``base``) or a repo root (a leading ``cd``, resolved against
    ``base``), with nothing that narrows collection (--ignore, --noconftest,
    -o testpaths=, stepwise...). ``pytest tests/unit`` or ``cd backend &&
    pytest`` inside a repo is not: a conftest broken elsewhere would not show."""
    import os
    import re
    import shlex
    m = re.match(r"\s*cd\s+(\S+)\s*&&\s*", cmd or "")
    if m:
        if not base:
            return False
        target = os.path.realpath(os.path.join(base, os.path.expanduser(m.group(1))))
        if target != os.path.realpath(base):
            from aiforge_core.runtime.repo_ident import git_toplevel
            top = git_toplevel(target)
            if not top or os.path.realpath(top) != target:
                return False      # a folder inside a repo, not its root
        cmd = cmd[m.end():]
    if re.search(r"&&|\|\||[;|`]|\$\(", cmd or ""):
        return False
    try:
        words = shlex.split(cmd)
    except ValueError:
        return False
    at = next((i for i, w in enumerate(words) if w == "pytest" or w.endswith("/pytest")), None)
    if at is None:
        return False
    skip = False
    for w in words[at + 1:]:
        if skip:
            skip = False
            continue
        if w.startswith(_NOT_WHOLE) and w.split("=")[0] in _NOT_WHOLE:
            return False
        if w in ("-o", "--override-ini") or w.startswith(("-o", "--override-ini=")):
            return False          # can reset testpaths / python_files
        if w in _VALUE_OPTS:
            skip = True
            continue
        if w.startswith("-"):
            continue
        if w.rstrip("/") not in _TOP_TESTS:
            return False
    return True


def _verify_known_ok(st, ok, out):
    """``(ok, out)`` with a run whose only failures the request itself calls
    known (``_only_known_failures``) counted as passing — the same verdict
    the green fingerprint, the done watch and the item log give it."""
    if ok is False and _only_known_failures(st, {"ok": False, "stdout": out}):
        return True, out
    if ok is False and not lint_only(out):
        # Red only for tests the original code fails too: the turn's own full
        # test command, run again, with each failure checked on a worktree of
        # the starting commit (uc-feature: 3 failures already in the repo,
        # two fix rounds and 4 of the run's 6 minutes spent on them).
        green = _own_green(st)
        if green is None:
            green = _ids_green(st, out)
        if green is not None:
            return True, green
    return ok, out


def _ids_green(st, out) -> "str | None":
    """The harness's red run when every test it names as failing fails the
    same way on the commit the work started from: those tests alone, run with
    the turn's own pytest interpreter (uc-feature: the turn only ran
    tests/test_helper.py, so no whole-suite command could vouch; 3 tests the
    repo already failed cost two fix rounds and three 80-s suite runs)."""
    import re as _re
    last = getattr(st, "last_test_cmd", None)
    text = out or ""
    from ._done_watch import failing_ids
    got = failing_ids(text)
    if got is None:
        return None                       # no summary: the list may be cut
    fails, errs = got
    ids = sorted(fails | errs)
    if not last or not ids or len(ids) > 40 or getattr(st, "own_red_at", None):
        return None
    # every failure named: the summary's counts are the listed ids, and no
    # collection error (no ::) hides among them
    n_f = _re.findall(r"\b(\d+) failed\b", text)
    n_e = _re.findall(r"\b(\d+) errors?\b", text)
    if (n_f and int(n_f[-1]) != len(fails)) or (n_e and int(n_e[-1]) != len(errs)) \
            or _re.search(r"^ERROR\s+(?!\S+::)\S+", text, _re.M):
        return None
    try:
        from aiforge_core.runtime import git_target
        from aiforge_core.runtime.shell_writes import command_of
        from ._contract_gate import work_base
        from ._done_watch import _interpreter, pre_existing
        py = _interpreter(command_of(last[1]) or "")
        base = work_base(st)
        if not py or not base:
            return None
        cwd = str(getattr(st, "cwd", "") or ".")
        here = git_target.command_base(cwd) or cwd
        key = (content_fingerprint(cwd) or f"edits:{getattr(st, 'edits_made', 0)}", tuple(ids))
        cached = getattr(st, "ids_green_at", None)
        if cached and cached[0] == key:
            return cached[1]
        import shlex
        cmd = f"{shlex.quote(py)} -m pytest -q -p no:cacheprovider " + " ".join(
            shlex.quote(i) for i in ids)
        # only the named tests: the suite's error total cannot match a rerun of three
        verdict = out if pre_existing(here, cmd, text, base, only_failing=True) else None
        st.ids_green_at = (key, verdict)
        return verdict
    except Exception:  # noqa: BLE001 — the runner's verdict stands
        return None


def _own_green(st) -> "str | None":
    """The output of the turn's own full test command when it is green or
    red only for failures the original code has (``own_test_verdict``).
    Only a whole-suite command stands for the tree, and only against the
    commit the work started from (never a HEAD that may hold the turn's own
    commit); once per code state and command."""
    last = getattr(st, "last_full_test", None)
    try:
        from aiforge_core.runtime import git_target
        from aiforge_core.runtime.shell_writes import command_of
        from ._contract_gate import work_base
        cmd = command_of(last[1]) if last else ""
        cwd = str(getattr(st, "cwd", "") or ".")
        if not cmd or not _whole_suite(cmd, git_target.command_base(cwd) or cwd):
            return None
        base = work_base(st)
        if not base:
            return None
    except Exception:  # noqa: BLE001
        return None
    here = (content_fingerprint(cwd) or f"edits:{getattr(st, 'edits_made', 0)}", cmd)
    cached = getattr(st, "own_green_at", None)
    if cached and cached[0] == here:
        return cached[1]
    out = None
    try:
        from ._done_watch import own_test_verdict
        if not getattr(st, "dw_base_rev", None):
            st.dw_base_rev = base         # pre_existing compares against this
        v = own_test_verdict(st)
        if v and v[0]:
            out = v[1]
        elif v is not None:
            st.own_red_at = here           # the turn's own whole suite says red
    except Exception:  # noqa: BLE001 — the runner's verdict stands
        out = None
    st.own_green_at = (here, out)
    return out


def still_failing_note(fails, output: str) -> str:
    """The line put in front of an answer when the project's own checks, run
    by the harness as the turn ends, still fail."""
    rows = [ln.strip() for ln in str(output or "").splitlines() if ln.strip()]
    detail = (rows[-1].strip("= ")[:160] if rows
              else (f"{fails} failing" if fails else ""))
    return ("⚠ The project's checks still FAIL as this turn ends"
            + (f" ({detail})" if detail else "")
            + ". What follows is not a verified result.\n\n")
