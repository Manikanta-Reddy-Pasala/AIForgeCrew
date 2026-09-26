"""The chat side of the no-progress rule (:mod:`aiforge_core.runtime.no_progress`):
what counts as progress for one tool step.

Progress is anything that moves the run: a new workspace state, a file or
page read for the first time (by a read tool, or ``cat``/``sed -n`` on a new
path), a read-only lookup it had not made, a task-board item marked done,
fewer failing tests (or a suite that turned green), or a running command
that is still producing output or finished. A step with none of these is
idle; enough idle steps that look alike, or enough in a row, is a loop.
"""
from __future__ import annotations

import collections
import contextlib

from aiforge_core.runtime import no_progress
from aiforge_core.runtime.failure_signature import failure_of, result_text

from .._registry import _READONLY_TOOLS
from ._progress import _SHELL_TOOLS, _refresh_tree, _remember

_CHECK_INS = ("command_wait", "command_output")
def _fields(st) -> None:
    if not hasattr(st, "np_mark"):
        st.np_mark = (0, 0, 0, 0, 0)
        st.np_seen = collections.OrderedDict()
        st.np_fails = None


def _mark(st) -> tuple:
    board = getattr(st, "board", None) or {}
    done = sum(1 for it in board.values() if it.get("status") == "done")
    return (getattr(st, "new_states", 0), getattr(st, "new_files", 0),
            getattr(st, "reads_new", 0), getattr(st, "edits_made", 0), done)


def _new(st, key: str) -> bool:
    if key in st.np_seen:
        return False
    _remember(st.np_seen, key)
    return True


def _shell_reads_new_path(st, args) -> bool:
    return any([_new(st, f"path:{p}") for p in no_progress.shell_read_paths(args)])


def _signals(st, name, args, result) -> bool:
    """Progress this step made that the loop state does not count itself."""
    res = result if isinstance(result, dict) else {}
    if name in _CHECK_INS and (res.get("output_growing") or res.get("cpu_active")
                               or res.get("running") is False):
        return True                   # a build being waited on is moving
    if name in _SHELL_TOOLS:
        progressed = _shell_reads_new_path(st, args)
        fail = failure_of(result_text(res)) if res.get("ok") is False else None
        if fail is not None and fail.signature:
            if st.np_fails is not None and fail.count < st.np_fails:
                progressed = True     # fewer failing tests
            st.np_fails = fail.count
        elif res.get("ok") is True and st.np_fails:
            st.np_fails, progressed = 0, True      # red turned green
        return progressed
    if name in _READONLY_TOOLS:
        return _new(st, no_progress.command_template(name, args))
    return False


def note_step(st, name, args, result):
    """Feed one tool step to the no-progress rule. Returns None,
    ``("nudge", text)`` or ``("stop", text)``."""
    track = getattr(st, "same_fail", None)
    if track is None:
        return None
    _fields(st)
    before, st.np_mark = st.np_mark, _mark(st)
    progress = _signals(st, name, args, result) or st.np_mark != before
    tmpl = no_progress.command_template(name, args)
    out = no_progress.output_class(name, result_text(result))
    if not progress and getattr(st, "tree_pending", False) and \
            no_progress.would_trip(track, tmpl, out):
        # Only now is it worth asking git whether the shell changed files.
        with contextlib.suppress(OSError, ValueError, TypeError):
            _refresh_tree(st)
        now = _mark(st)
        progress, st.np_mark = now != st.np_mark, now
    verdict = no_progress.observe(track, tmpl, out, progress)
    reason = str(track.get("last") or "")
    if verdict == no_progress.NUDGE:
        return verdict, no_progress.nudge_text(reason)
    if verdict == no_progress.STOP:
        return verdict, no_progress.stop_text(reason)
    return None
