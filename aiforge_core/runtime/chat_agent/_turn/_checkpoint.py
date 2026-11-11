"""A way back from a send-back: the answer's code is kept, and restored when
the extra round leaves the project's tests worse.

attrs-inspect: 13/18 when a finish check sent the answer back for tests; the
extra round moved a class below its first use at import time and the run
ended at 0/18, the whole suite failing. Every check that sends a finished
answer back (missing tests, examples, the project's own verify) asks for
more work on code that was, at that moment, an answer.

The first time a finish check sends the answer back, the working tree is
written to a git tree object (a throwaway index, so the user's index and
files are untouched; untracked files included, .gitignore respected) and the
project's tests are run once to count the failures. When the turn answers
(normally or at the time limit), the tests run again; more failures, or a
suite that no longer runs, put the saved tree back and say so in the answer.
Files the extra round created are removed; nothing outside the repo is
touched. Off switch: ``AIFORGE_CHAT_CHECKPOINT=0``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile

log = logging.getLogger("aiforge.chat.checkpoint")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_CHECKPOINT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(top: str, *args, env=None, timeout: int = 60) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(["git", "--no-optional-locks", "-C", top, *args],
                              capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
    except (OSError, subprocess.SubprocessError):
        return None


def _top(cwd) -> str:
    r = _git(str(cwd or "."), "rev-parse", "--show-toplevel", timeout=10)
    return r.stdout.strip() if r is not None and r.returncode == 0 else ""


def tree_of(top: str) -> str:
    """The working tree as a git tree id (untracked files included)."""
    with tempfile.TemporaryDirectory(prefix="aiforge-ckpt-") as d:
        env = {"GIT_INDEX_FILE": os.path.join(d, "index")}
        head = _git(top, "rev-parse", "--verify", "-q", "HEAD^{tree}", timeout=10)
        if head is not None and head.returncode == 0:
            _git(top, "read-tree", head.stdout.strip(), env=env)
        add = _git(top, "add", "-A", ".", env=env, timeout=120)
        if add is None or add.returncode != 0:
            return ""
        w = _git(top, "write-tree", env=env)
        return w.stdout.strip() if w is not None and w.returncode == 0 else ""


def restore_tree(top: str, tree: str) -> bool:
    """Make the working tree match ``tree``: its files written back, files
    added since removed. The user's index is not used."""
    now = tree_of(top)
    if not now:
        return False
    added = _git(top, "diff", "--name-only", "--no-renames", "--diff-filter=A", tree, now)
    with tempfile.TemporaryDirectory(prefix="aiforge-ckpt-") as d:
        env = {"GIT_INDEX_FILE": os.path.join(d, "index")}
        r = _git(top, "read-tree", tree, env=env)
        if r is None or r.returncode != 0:
            return False
        c = _git(top, "checkout-index", "-a", "-f", env=env, timeout=120)
        if c is None or c.returncode != 0:
            return False
    for rel in (added.stdout.splitlines() if added is not None and added.returncode == 0 else []):
        p = os.path.realpath(os.path.join(top, rel))
        if p.startswith(os.path.realpath(top) + os.sep) and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return True


def _failures(st, cwd) -> "int | None":
    """Failing tests by the project's own finish check: 0 green, 999 the
    suite does not run, None no verdict (nothing to compare)."""
    try:
        from aiforge_core.runtime.parallel_subtasks import _fail_count
        from ._outcomes import _project_verify
        ok, out = _project_verify(st, cwd)
    except Exception as exc:  # noqa: BLE001 — no verdict, no checkpoint
        log.debug("checkpoint verify failed: %s", exc)
        return None
    if ok is True:
        return 0
    if ok is None:
        return None
    return _fail_count(out or "")


def save(st, cwd, reason: str = "a finish check") -> None:
    """At the first send-back of a finished answer: keep it, with its count."""
    if not _on() or getattr(st, "checkpoint", None) is not None \
            or getattr(st, "edits_made", 0) <= 0:
        return
    st.checkpoint = {}                    # tried once per turn, whatever happens
    top = _top(cwd)
    if not top:
        return
    tree = tree_of(top)
    if not tree:
        return
    fails = _failures(st, cwd)
    if fails is None:
        return
    st.checkpoint = {"top": top, "tree": tree, "fails": fails, "reason": reason}


def restore_if_worse(st, cwd) -> str:
    """At the answer: the note to put first when the saved state came back,
    else ""."""
    cp = getattr(st, "checkpoint", None) or {}
    if not _on() or not cp.get("tree"):
        return ""
    top = cp["top"]
    if tree_of(top) == cp["tree"]:
        return ""                         # nothing changed since: nothing to judge
    now = _failures(st, cwd)
    if now is None or now <= cp["fails"]:
        return ""
    if not restore_tree(top, cp["tree"]):
        return ""
    st.checkpoint = {**cp, "restored": True}
    def said(n):
        return "the tests could not run" if n >= 999 else f"{n} failing test(s)"
    return (f"⚠ **The harness put the code back to how it was before {cp['reason']}.** "
            f"The extra work after it left {said(now)} (before: {said(cp['fails'])}); "
            "the answer below may describe changes that are no longer there.\n\n")


__all__ = ["restore_if_worse", "restore_tree", "save", "tree_of"]
