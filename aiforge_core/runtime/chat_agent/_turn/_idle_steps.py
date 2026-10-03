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
from aiforge_core.runtime.failure_signature import result_text

from .._registry import _READONLY_TOOLS
from ._progress import _SHELL_TOOLS, _refresh_tree

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
    return no_progress.note_new(st.np_seen, key)


def _in_workspace(st, path: str) -> bool:
    """A path of the task: inside the run's folder (or one the user named),
    and not git's own bookkeeping.

    Live: a run that could not make a test pass spent forty steps reading
    other projects' folders, other chats' worktrees and ``.git`` internals.
    Each was a path it had not read before, so each reset the count and the
    no-progress rule never fired in 39 minutes. Reading the rest of the
    machine is not progress on the task."""
    import os
    roots = [r for r in [getattr(st, "cwd", None),
                         *(getattr(st, "user_roots", None) or [])] if r]
    if not roots:
        return True                       # no workspace known: as before
    full = os.path.realpath(os.path.join(str(roots[0]), os.path.expanduser(path)))
    if ".git" in full.split(os.sep):
        return False
    for root in roots:
        root = os.path.realpath(str(root))
        if full == root or full.startswith(root + os.sep):
            return True
    return False


def _shell_reads_new_path(st, args) -> bool:
    return any([_new(st, f"path:{p}") for p in no_progress.shell_read_paths(args)
                if _in_workspace(st, p)])


def _fails_moved(st, name, args, res) -> bool:
    """Fewer failing tests, or red turned green, in a FINISHED run (see
    :func:`no_progress.fails_moved`)."""
    moved, st.np_fails = no_progress.fails_moved(st.np_fails, name, args, res)
    return moved


def _signals(st, name, args, result) -> bool:
    """Progress this step made that the loop state does not count itself."""
    res = result if isinstance(result, dict) else {}
    progressed = False
    if name in _CHECK_INS and (res.get("output_growing") or res.get("cpu_active")
                               or res.get("running") is False):
        progressed = True             # a build being waited on is moving
    if name in _SHELL_TOOLS:
        progressed = _shell_reads_new_path(st, args) or progressed
    if name in _SHELL_TOOLS or name in _CHECK_INS or name == "run_tests":
        return _fails_moved(st, name, args, res) or progressed
    if name in _READONLY_TOOLS:
        return (_new(st, no_progress.command_template(name, args))
                and read_is_news(st, result))
    return False


#: Result fields that say where a read looked, not what it found.
_NOT_CONTENT = frozenset({"ok", "path", "paths", "start", "end", "total_lines",
                          "note", "hint", "truncated", "bytes", "count", "read",
                          "failed", "by"})
_MAX_LINES_SEEN = 200_000


def _lines_of(value, out: list) -> None:
    if isinstance(value, str):
        out.extend(ln.strip() for ln in value.splitlines())
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in _NOT_CONTENT:
                _lines_of(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _lines_of(item, out)


def read_is_news(st, result) -> bool:
    """Whether a read returned at least one line this run had not seen.

    A read used to count as new knowledge when its ARGUMENTS were new. Live,
    a run that could not make a test pass read the same six-line file 75
    times, asking for lines 1-7, 1-8 … 1-71: every call was "a new read", so
    it was progress, and no rule fired in 14 minutes. The same lines again
    are not news, whatever range was asked for. Asked twice about one result
    (the read counter and the idle rule both ask), it answers the same."""
    cached = getattr(st, "_read_news", None)
    if cached is not None and cached[0] is result:
        return cached[1]
    seen = getattr(st, "read_lines_seen", None)
    if seen is None:
        seen = st.read_lines_seen = collections.OrderedDict()
    lines: list = []
    _lines_of(result, lines)
    news = False
    for line in lines:
        if line and no_progress.note_new(seen, line):
            news = True
    while len(seen) > _MAX_LINES_SEEN:
        seen.popitem(last=False)
    st._read_news = (result, news)
    return news


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
