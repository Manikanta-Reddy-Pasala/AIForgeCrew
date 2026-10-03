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
finding new ways to be stuck. Neither signal can tell a real fix from a
different wrong one; the same-failure rule (``_outcomes``) does.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os

from aiforge_core.runtime.tools.mutating import writes_files

from aiforge_core.runtime.stuck_policy import Policy

from .._context import _LOOP_REPEAT
from ._stuck.detect import (  # noqa: F401  # re-exported: tests and callers import them from here
    bump_identical,
    identical_repeats,
    note_identical,
    note_monologue,
    ping_pong,
    reset_monologue,
    similar_text,
)
from ._stuck.signal import short_key as _short
from ._stuck.state import StuckState, reset

#: Paths, states and strike keys kept per run; the oldest are forgotten.
_MAX_TRACKED = 20_000
_PATH_KEYS = ("path", "file", "file_path", "target", "dest", "new_path")
_PATH_LIST_KEYS = ("paths", "files")
_NO_FILE = "-"
_SHELL_TOOLS = frozenset({"run_command", "bash", "shell", "run", "run_shell"})


def progress_fields() -> dict:
    """The loop-state fields this module keeps."""
    own = StuckState()
    return {
        "file_hashes": collections.OrderedDict(),
        "state_fp": "",
        "states_seen": collections.OrderedDict({"": True}),
        "paths_read": collections.OrderedDict(),
        "strikes": own.strikes,
        "backstop": own.backstop,
        "lifetime": own.lifetime,
        "tree_pending": False,
        "tree_hashes": {},
        "tree_cache": collections.OrderedDict(),
        "unknown_edits": 0,
        "git_root": None,
        "git_off": False,
        "new_states": 0,
        "new_files": 0,
        "recoveries_total": own.recoveries_total,
        "recovery_mark": own.recovery_mark,
        "lifetime_forgiven": own.lifetime_forgiven,
        "same_fail": own.same_fail,
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
    known = bool(files) and not any(os.path.isdir(f) for f in files)
    if known:
        for full in files:
            _remember(st.file_hashes, full, _digest(full))
        _note_repo_focus(st, files, cwd, 2.0)
    else:
        # A tool that does not say which files it touched, or names a folder
        # (a rename across the tree): nothing to compare, so a new state.
        st.unknown_edits += 1
    _enter_state(st, _state_of(st), known=known)
    return True


def _note_repo_focus(st, files, cwd, weight) -> None:
    """Tell the ranked repo map which files the run touched (personalisation)."""
    try:
        from aiforge_core.runtime.chat_agent._context import _repomap_rank as rr
        from aiforge_core.runtime.chat_agent._shell import _workspace_root
        base = str(_workspace_root() or cwd or "")
        if base:
            rr.note_focus(getattr(st, "session_id", None), base, files, weight)
    except Exception:  # noqa: BLE001 — personalisation is best effort
        pass


def _state_of(st) -> str:
    """The workspace state: content of the files written by tools, content
    of the files the shell changed or created, and edits that named no file."""
    state = json.dumps([sorted(st.file_hashes.items()),
                        sorted(st.tree_hashes.items()), st.unknown_edits])
    return hashlib.sha1(state.encode()).hexdigest()  # noqa: S324


def _enter_state(st, fp: str, known: bool = True) -> None:
    st.state_fp = fp
    if fp not in st.states_seen:
        st.new_states += 1
        if known:
            # somewhere new that we can name: repeats start over. An edit
            # that named no file is always "new", so it cannot do this.
            st.backstop.clear()
    _remember(st.states_seen, fp)


#: Changed files looked at per command; a tree with more is still a change.
_MAX_CHANGED = 500
_GIT_TIMEOUT_S = 5


def _git(st, cwd, *args):
    import subprocess
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0",
           # never climb into a repository at or above the home directory
           "GIT_CEILING_DIRECTORIES": os.path.expanduser("~")}
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                             capture_output=True, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        st.git_off = True             # too slow here: stop asking this run
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _tracked_changes(st, cwd) -> dict | None:
    """Content hashes of the files git reports as changed or new (an ignored
    file is never listed), or None outside a repository. A file is re-hashed only when its size or
    modification time moved."""
    if st.git_off:
        return None
    if st.git_root is None:
        top = _git(st, cwd, "rev-parse", "--show-toplevel")
        st.git_root = top.decode("utf-8", "replace").strip() if top else ""
    if not st.git_root:
        return None
    # "normal" lists an untracked folder once, so a tree whose node_modules is
    # not ignored costs one entry, not a walk; such a folder is fingerprinted
    # by its direct entries.
    raw = _git(st, cwd, "status", "--porcelain", "-z", "--untracked-files=normal")
    if raw is None:
        return None
    entries = raw.split(b"\0")
    changed: dict = {}
    i = 0
    while i < len(entries) and len(changed) < _MAX_CHANGED:
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        if entry[:1] in (b"R", b"C"):
            i += 1                    # the rename's old path follows
        path = os.path.join(st.git_root, entry[3:].decode("utf-8", "replace"))
        changed[path] = (_folder_digest(path) if os.path.isdir(path)
                         else _cached_digest(st, path))
    return changed


def _folder_digest(path: str) -> str:
    """Names, sizes and times of a folder's direct entries (not a walk)."""
    try:
        with os.scandir(path) as it:
            rows = sorted(f"{e.name}:{e.stat().st_size}:{e.stat().st_mtime_ns}"
                          for e in it)
    except OSError:
        return _NO_FILE
    return hashlib.sha1("|".join(rows[:_MAX_CHANGED]).encode()).hexdigest()  # noqa: S324


def _cached_digest(st, path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return _NO_FILE
    key = (stat.st_size, stat.st_mtime_ns)
    hit = st.tree_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    digest = _digest(path)
    _remember(st.tree_cache, path, (key, digest))
    return digest


def note_command(st, name, result, cwd) -> None:
    """A shell command can change files too (sed -i, git apply, codegen).
    After a successful one the tree is looked at again — lazily, when a
    repeat is about to count as a loop (see :func:`strike`)."""
    if name in _SHELL_TOOLS and isinstance(result, dict) and result.get("ok"):
        st.tree_pending = True
        st.tree_cwd = cwd


def _refresh_tree(st) -> None:
    """Fold the files the shell changed or created into the state. Rewriting a
    file with the same bytes, or flipping it back, is not new."""
    st.tree_pending = False
    changed = _tracked_changes(st, getattr(st, "tree_cwd", None) or getattr(st, "cwd", ""))
    if changed is None or changed == st.tree_hashes:
        return
    st.tree_hashes = changed
    _enter_state(st, _state_of(st))


def note_read(st, args, result, cwd=None) -> None:
    """Record the files a landed read covered."""
    if isinstance(result, dict) and result.get("ok") is False:
        return
    paths = [_full(x, cwd) for x in _named_paths(args, None)]
    for p in paths:
        if p not in st.paths_read:
            st.new_files += 1
        _remember(st.paths_read, p)
    _note_repo_focus(st, paths, cwd, 1.0)


def count_key(name: str, sig: str) -> str:
    """The loop's per-action count key: the tool name, readable, plus a hash
    of the arguments (which can hold a whole file)."""
    return f"{name}|{_short(sig)[:16]}"


def strike(st, sig, per_state: bool = True) -> str:
    """Count one more run of ``sig``. Returns "" when it is fine, "same" when
    it repeats in an unchanged workspace, "often" when it has simply run too
    many times.

    Three counts: repeats in the current workspace state (``per_state``);
    repeats since the workspace last reached a new state through a known
    edit (the backstop); and repeats in the whole run, which nothing resets
    but a recovery (the lifetime ceiling, 3 × the backstop)."""
    sig = _short(sig)
    _remember(st.lifetime, sig, st.lifetime.get(sig, 0) + 1)
    _remember(st.backstop, sig, st.backstop.get(sig, 0) + 1)
    if st.tree_pending and _over(st, sig, per_state, extra=1):
        # Only now is it worth asking git whether a shell command changed
        # files: most commands never come near a loop.
        _refresh_tree(st)
    if per_state:
        key = f"{sig}@{st.state_fp}"
        _remember(st.strikes, key, st.strikes.get(key, 0) + 1)
        if st.strikes[key] >= _LOOP_REPEAT:
            return "same"
    return "often" if _over(st, sig, False) else ""


def _over(st, sig, per_state, extra=0) -> bool:
    if per_state and st.strikes.get(f"{sig}@{st.state_fp}", 0) + extra >= _LOOP_REPEAT:
        return True
    backstop = Policy.load().backstop
    return (st.backstop.get(sig, 0) >= backstop
            or st.lifetime.get(sig, 0) >= 3 * backstop)


def forgive(st, sig) -> None:
    """After a recovery nudge, give this action a fresh count. The lifetime
    count is reset once per action: a second reset would make the ceiling
    one that nothing holds."""
    reset(st, "recovery")
    sig = _short(sig)
    backstop = Policy.load().backstop
    st.strikes[f"{sig}@{st.state_fp}"] = 0
    if st.backstop.get(sig, 0) >= backstop:
        st.backstop[sig] = 0
    if (st.lifetime.get(sig, 0) >= 3 * backstop
            and sig not in st.lifetime_forgiven):
        _remember(st.lifetime_forgiven, sig)
        st.lifetime[sig] = 0


def _closed_items(st) -> int:
    """Items the run FINISHED. Giving up on one ("failed", "skipped") is not
    progress, so it does not refill the recovery ceiling."""
    board = getattr(st, "board", None) or {}
    return sum(1 for it in board.values() if it.get("status") == "done")


def may_recover(st) -> bool:
    """Spend one stuck recovery, or False when the run should stop and ask.

    The per-stall budget refills once the workspace reached a state it had
    never been in, or the run read a file it had not read; the per-run
    ceiling refills only when one more task-board item is done."""
    if st.tree_pending:
        _refresh_tree(st)
    mark = (st.new_states, st.new_files)
    if st.recovery_mark is not None and mark != st.recovery_mark:
        st.stuck_recoveries = 0
    st.recovery_mark = mark
    closed = _closed_items(st)
    mark = getattr(st, "recoveries_closed_mark", None)
    if mark is not None and closed > mark:
        st.recoveries_total = 0       # a newly finished task is real progress
    st.recoveries_closed_mark = max(closed, mark or 0)
    pol = Policy.load()
    if (st.stuck_recoveries >= pol.stuck_recoveries
            or st.recoveries_total >= pol.max_recoveries):
        return False
    st.stuck_recoveries += 1
    st.recoveries_total += 1
    return True
