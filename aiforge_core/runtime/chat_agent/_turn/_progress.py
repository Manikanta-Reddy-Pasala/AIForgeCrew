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
    """Stuck recoveries one run may use in total
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


def _digest(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()  # noqa: S324  # not security
    except OSError:
        return _NO_FILE


def note_write(st, name, args, result, cwd) -> bool:
    """Record a landed write; True when it counts as an edit."""
    if not writes_files(name, args if isinstance(args, dict) else {}):
        return False
    if isinstance(result, dict) and result.get("ok") is False:
        return False
    paths = _named_paths(args, result)
    if paths:
        for p in paths:
            full = os.path.normpath(os.path.join(str(cwd or ""), os.path.expanduser(p)))
            _remember(st.file_hashes, full, _digest(full))
        state = json.dumps(sorted(st.file_hashes.items()))
    else:
        # A tool that does not say which files it touched (a rename across
        # the tree): nothing to compare, so it is a new state.
        state = f"{st.state_fp}#{len(st.states_seen)}"
    st.state_fp = hashlib.sha1(state.encode()).hexdigest()  # noqa: S324
    if st.state_fp not in st.states_seen:
        st.new_states += 1
    _remember(st.states_seen, st.state_fp)
    return True


def note_read(st, args, result) -> None:
    """Record the files a landed read covered."""
    if isinstance(result, dict) and result.get("ok") is False:
        return
    for p in _named_paths(args, None):
        if p not in st.paths_read:
            st.new_files += 1
        _remember(st.paths_read, p)


def strike(st, sig) -> bool:
    """Count one more run of ``sig``; True when it now counts as a loop."""
    key = f"{sig}@{st.state_fp}"
    _remember(st.strikes, key, st.strikes.get(key, 0) + 1)
    return (st.strikes[key] >= _LOOP_REPEAT
            or st.action_counts.get(sig, 0) >= loop_backstop())


def forgive(st, sig) -> None:
    """After a recovery nudge, give this action a fresh count."""
    st.strikes[f"{sig}@{st.state_fp}"] = 0
    if st.action_counts.get(sig, 0) >= loop_backstop():
        st.action_counts[sig] = 0


def may_recover(st) -> bool:
    """Spend one stuck recovery, or False when the run should stop and ask.

    The per-stall budget refills once the workspace reached a state it had
    never been in, or the run read a file it had not read; the per-run
    ceiling never refills."""
    mark = (st.new_states, st.new_files)
    if st.recovery_mark is not None and mark != st.recovery_mark:
        st.stuck_recoveries = 0
    st.recovery_mark = mark
    if (st.stuck_recoveries >= _stuck_recovery_max()
            or st.recoveries_total >= max_recoveries()):
        return False
    st.stuck_recoveries += 1
    st.recoveries_total += 1
    return True
