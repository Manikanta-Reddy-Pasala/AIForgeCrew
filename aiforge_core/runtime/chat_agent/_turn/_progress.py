"""Telling a long run that is moving from one that is going round in circles.

Two signals, both hard to fake by accident:

* the workspace state — a fingerprint of the content of every file the run
  has written. Re-running a command after a real change is a new action; an
  edit that changes nothing, or flips a file back to an earlier version, is
  not new;
* new knowledge — files read for the first time.

The loop guard counts repeats of an action per workspace state (with a
lifetime backstop), and the stuck-recovery budget refills only when one of the
signals moved. A hard per-run ceiling on recoveries stops a run that keeps
finding new ways to be stuck.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os

from aiforge_core.runtime.tools.mutating import writes_files

from .._context import _LOOP_REPEAT, _stuck_recovery_max

#: Paths, states and strike keys kept per run; the oldest are forgotten.
_MAX_TRACKED = 20_000
_PATH_KEYS = ("path", "file", "file_path", "target", "dest", "new_path")
_PATH_LIST_KEYS = ("paths", "files")
_NO_FILE = "-"
_SHELL_TOOLS = frozenset({"run_command", "bash", "shell", "run", "run_shell"})


def _int_env(name: str, default: int) -> int:
    try:
        val = int(os.environ.get(name, default))
    except ValueError:
        return default
    return val if val > 0 else default


def loop_backstop() -> int:
    """Repeats of one action, across every workspace state, that count as a
    loop anyway (``AIFORGE_CHAT_LOOP_BACKSTOP``, default 30)."""
    return _int_env("AIFORGE_CHAT_LOOP_BACKSTOP", 30)


def max_recoveries() -> int:
    """Stuck recoveries one run may use between two closed task-board items
    (``AIFORGE_CHAT_MAX_RECOVERIES``, default 30)."""
    return _int_env("AIFORGE_CHAT_MAX_RECOVERIES", 30)


def progress_fields() -> dict:
    """The loop-state fields this module keeps."""
    return {
        "file_hashes": collections.OrderedDict(),
        "state_fp": "",
        "states_seen": collections.OrderedDict({"": True}),
        "paths_read": collections.OrderedDict(),
        "strikes": collections.OrderedDict(),
        "backstop": collections.OrderedDict(),
        "git_fp": None,
        "new_states": 0,
        "new_files": 0,
        "recoveries_total": 0,
        "recovery_mark": None,
    }


def _remember(table, key, value=True):
    table[key] = value
    table.move_to_end(key)
    while len(table) > _MAX_TRACKED:
        table.popitem(last=False)


def _named_paths(args, result) -> list[str]:
    found: list[str] = []
    for src in (args, result):
        if not isinstance(src, dict):
            continue
        found += [str(src[k]) for k in _PATH_KEYS if isinstance(src.get(k), str) and src[k]]
        for k in _PATH_LIST_KEYS:
            if isinstance(src.get(k), list):
                found += [str(p) for p in src[k] if isinstance(p, str) and p]
        edits = src.get("edits")
        if isinstance(edits, list):
            found += [e["path"] for e in edits
                      if isinstance(e, dict) and isinstance(e.get("path"), str)]
    return found


def _full(path: str, cwd) -> str:
    return os.path.realpath(os.path.join(str(cwd or ""), os.path.expanduser(path)))


def _digest(path: str) -> str:
    digest = hashlib.sha1()  # noqa: S324  # not security
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return _NO_FILE
    return digest.hexdigest()


def note_write(st, name, args, result, cwd) -> bool:
    """Record a landed write; True when it counts as an edit."""
    if not writes_files(name, args if isinstance(args, dict) else {}):
        return False
    if isinstance(result, dict) and result.get("ok") is False:
        return False
    paths = _named_paths(args, None) or _named_paths(None, result)
    files = [_full(p, cwd) for p in paths]
    if files and not any(os.path.isdir(f) for f in files):
        for full in files:
            _remember(st.file_hashes, full, _digest(full))
        state = json.dumps(sorted(st.file_hashes.items()))
    else:
        # A tool that does not say which files it touched, or names a folder
        # (a rename across the tree): nothing to compare, so a new state.
        state = f"{st.state_fp}#{len(st.states_seen)}"
    _enter_state(st, hashlib.sha1(state.encode()).hexdigest())  # noqa: S324
    return True


def _enter_state(st, fp: str) -> None:
    st.state_fp = fp
    if fp not in st.states_seen:
        st.new_states += 1
        st.backstop.clear()           # somewhere new: repeats start over
    _remember(st.states_seen, fp)


#: Changed files looked at per command; a tree with more is still a change.
_MAX_CHANGED = 500


def _tree_state(cwd) -> str:
    """A fingerprint of the files git reports as changed: their names, sizes
    and modification times. "" outside a git repo."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=str(cwd), capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    entries = [e for e in out.stdout.split(b"\0") if e]
    parts = [out.stdout[:0]]
    for e in entries[:_MAX_CHANGED]:
        path = os.path.join(str(cwd), e[3:].decode("utf-8", "replace"))
        try:
            s = os.stat(path)
            parts.append(f"{e[:2]!r}{path}:{s.st_size}:{s.st_mtime_ns}".encode())
        except OSError:
            parts.append(e)
    parts.append(str(len(entries)).encode())
    return hashlib.sha1(b"\n".join(parts)).hexdigest()  # noqa: S324


def note_command(st, name, result, cwd) -> None:
    """A shell command can change files too (sed -i, git apply, codegen). After
    a successful one, fold the git view of the tree into the state, so an
    edit-by-shell-then-test cycle is progress, not a loop."""
    if name not in _SHELL_TOOLS or not (isinstance(result, dict) and result.get("ok")):
        return
    fp = _tree_state(cwd)
    if not fp or fp == st.git_fp:
        return
    st.git_fp = fp
    _enter_state(st, hashlib.sha1(f"{st.state_fp}|{fp}".encode()).hexdigest())  # noqa: S324


def note_read(st, args, result, cwd=None) -> None:
    """Record the files a landed read covered."""
    if isinstance(result, dict) and result.get("ok") is False:
        return
    for p in (_full(x, cwd) for x in _named_paths(args, None)):
        if p not in st.paths_read:
            st.new_files += 1
        _remember(st.paths_read, p)


def _short(sig: str) -> str:
    """A fixed-size key: a signature holds the whole call, file content too."""
    return hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()  # noqa: S324


def strike(st, sig, per_state: bool = True) -> bool:
    """Count one more run of ``sig``; True when it now counts as a loop.

    Two counts: repeats in the current workspace state (``per_state``), and
    repeats since the workspace last reached a state it had never been in —
    the backstop for a run that changes files without getting anywhere."""
    looping = False
    sig = _short(sig)
    if per_state:
        key = f"{sig}@{st.state_fp}"
        _remember(st.strikes, key, st.strikes.get(key, 0) + 1)
        looping = st.strikes[key] >= _LOOP_REPEAT
    _remember(st.backstop, sig, st.backstop.get(sig, 0) + 1)
    return looping or st.backstop[sig] >= loop_backstop()


def forgive(st, sig) -> None:
    """After a recovery nudge, give this action a fresh count."""
    sig = _short(sig)
    st.strikes[f"{sig}@{st.state_fp}"] = 0
    if st.backstop.get(sig, 0) >= loop_backstop():
        st.backstop[sig] = 0


def _closed_items(st) -> int:
    board = getattr(st, "board", None) or {}
    return sum(1 for it in board.values()
               if it.get("status") in ("done", "failed", "skipped"))


def may_recover(st) -> bool:
    """Spend one stuck recovery, or False when the run should stop and ask.

    The per-stall budget refills once the workspace reached a state it had
    never been in, or the run read a file it had not read; the per-run
    ceiling never refills."""
    mark = (st.new_states, st.new_files)
    if st.recovery_mark is not None and mark != st.recovery_mark:
        st.stuck_recoveries = 0
    st.recovery_mark = mark
    closed = _closed_items(st)
    if closed != getattr(st, "recoveries_closed_mark", closed):
        st.recoveries_total = 0       # a finished task is real progress
    st.recoveries_closed_mark = closed
    if (st.stuck_recoveries >= _stuck_recovery_max()
            or st.recoveries_total >= max_recoveries()):
        return False
    st.stuck_recoveries += 1
    st.recoveries_total += 1
    return True
