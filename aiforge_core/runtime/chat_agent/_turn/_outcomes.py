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
    known = _only_known_failures(st, result)
    _gaming.note_test_result(st, True if known else result.get("ok"))
    if result.get("ok") is not True and not known:
        st.last_green_fp = None
        if _full_and_honest(name, args):
            st.last_full_test = (name, dict(args or {}))
        return
    if not _full_and_honest(name, args):
        return                        # proves nothing about the whole suite
    st.last_full_test = (name, dict(args or {}))
    text = result_text(result)
    if stale or not (known or (runner_green(text) and _RAN.search(text))):
        # a job checked on after edits landed tested older code; a build
        # with no test count ran no tests
        return
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
                        r"don'?t ignore|do not ignore)\b", re.I)


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
    # a Q&A turn (0 edits) or read-only plan mode is untouched. Keep
    # looping while the failure count DROPS; once it stalls (2 rounds no
    # improvement) accept the HONEST still-failing final rather than
    # churn. Opt out: AIFORGE_CHAT_VERIFY_ON_FINAL=0.
    # The agent already ran the whole suite green and the tree's content
    # has not changed since: running it again is the triple test run.
    if (getattr(st, "last_green_fp", None)
            and content_fingerprint(cwd) == st.last_green_fp):
        return None
    if (plan_mode or builder or st.edits_made <= 0
            or st.verify_rounds >= _verify_max_rounds()
            or not _verify_on_final_enabled()):
        return None
    yield {"type": "thought", "role": "system",
           "text": "⧗ running the project's checks before finishing…"}
    _vok, _vout = _run_project_verify(cwd)
    _gaming.note_test_result(st, _vok)
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


def still_failing_note(fails, output: str) -> str:
    """The line put in front of an answer when the project's own checks, run
    by the harness as the turn ends, still fail."""
    rows = [ln.strip() for ln in str(output or "").splitlines() if ln.strip()]
    detail = (rows[-1].strip("= ")[:160] if rows
              else (f"{fails} failing" if fails else ""))
    return ("⚠ The project's checks still FAIL as this turn ends"
            + (f" ({detail})" if detail else "")
            + ". What follows is not a verified result.\n\n")
