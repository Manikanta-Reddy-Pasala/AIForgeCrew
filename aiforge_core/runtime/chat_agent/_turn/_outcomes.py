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
)
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
    run = cmd_finished.as_run(name, args, result)
    if run is None:
        return
    name, args, result = run
    if not isinstance(result, dict) or not _is_test_run(name, args):
        return
    if result.get("ok") is not True:
        st.last_green_fp = None
        return
    if not (_full_and_honest(name, args) and runner_green(result_text(result))):
        return                        # proves nothing about the whole suite
    try:
        st.last_green_fp = content_fingerprint(cwd)
    except Exception:  # noqa: BLE001
        st.last_green_fp = None


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
    verdict = same_failure.observe(track, fail, _state_key(st))
    if verdict == same_failure.NUDGE:
        return verdict, same_failure.nudge_text(fail)
    if verdict == same_failure.STOP:
        return verdict, same_failure.stop_text(fail)
    return None


# ── verify on FINAL ──────────────────────────────────────────────────────

def _verify_on_final(st, step, cwd, plan_mode, builder):
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
        yield {"type": "thought", "role": "system",
               "text": f"✗ tests failing ({_fails}) — fixing "
                       f"(verify round {st.verify_rounds}/"
                       f"{_verify_max_rounds()})…"}
        msg = _verify_fix_message(_vout)
        if seen:
            msg += "\n\n" + seen[1]
        st.convo.append({"role": "user", "content": msg})
        return "continue"
    yield {"type": "thought", "role": "system",
           "text": f"⚠ tests still failing ({_fails}) after "
                   f"{st.verify_rounds} fix rounds — stopping with "
                   "the honest state."}
    return None
