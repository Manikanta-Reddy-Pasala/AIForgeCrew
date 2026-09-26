"""The safety net under a team run's shell commands: the user's REAL checkout
is put back if a command changed it.

A team run works in its own worktree, and team_repo_guard refuses the obvious
commands that write into the user's repo. A text check cannot be complete
(``env -C <repo>``, ``cd "$(…)"``, ``find <repo> | xargs sed -i``, a heredoc
``python3 - <<PY open('<repo>/x','w')``, ``cp -t <repo>``, ``rsync
--remove-source-files`` …), so every shell command of a team run is bracketed:

* :func:`begin` — before it runs: HEAD, the branch ref, the index, and
  ``git status --porcelain -z --untracked-files=all`` of the real repo, plus a
  copy of every dirty / untracked file (the user's own uncommitted work);
* :func:`end` — after it: if anything moved, it is put back. Tracked files
  get their pre-command content (the user's uncommitted edits exactly), new
  files are MOVED to a quarantine folder (never deleted), deleted files come
  back, the index is restored and a moved branch ref / HEAD is reset (the
  commit stays in the reflog). The tool result becomes
  ``{ok: false, error: "changed_users_checkout", …}``.

Only changes whose mtime falls inside the command's run window are undone —
the user may be editing their own repo while a long run goes on; anything
else is reported as changed outside the command and left alone. A command
that leaves a job running is checked again on every later tool call of the
run and when the run closes; a job that touched the repo is killed first.
Nothing happens outside a team run, or when the user granted the real repo.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_NET_TOOLS_EXTRA = frozenset({"command_wait", "command_output", "command_kill"})
_MAX_COPY = 50 * 1024 * 1024
_SLACK_S = 1.0
_LOCK = threading.Lock()
_PENDING: dict[str, list] = {}            # ws.cwd → [(job, snap, t0)]


@dataclass
class Snap:
    repo: str
    head: str
    ref: str                     # "refs/heads/main", or "" when detached
    ref_sha: str
    status: bytes
    entries: dict                # path → (xy, stat_sig, copy_path | None)
    index: str                   # the index file's path
    index_sig: tuple
    index_copy: str
    index_digest: str = ""
    t0: float = field(default_factory=time.time)


# ── git / fs helpers ───────────────────────────────────────────────────────

def _git(repo: str, *args: str, binary: bool = False):
    # --no-optional-locks: our own `status` must not rewrite the index.
    p = subprocess.run(["git", "--no-optional-locks", "-C", repo, *args],
                       capture_output=True, timeout=60, text=not binary)
    return p


def _out(repo: str, *args: str) -> str:
    p = _git(repo, *args)
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def _sig(path: str) -> tuple:
    try:
        st = os.lstat(path)
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size, st.st_mode)


def _status(repo: str) -> bytes:
    p = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
             binary=True)
    return p.stdout if p.returncode == 0 else b""


def _parse(status: bytes) -> dict:
    """``path → xy`` from ``status -z`` (a rename lists new and old)."""
    out: dict = {}
    parts = status.split(b"\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2].decode(), rec[3:].decode("utf-8", "surrogateescape")
        out[path] = xy
        if xy[0] in "RC" and i < len(parts):
            out[parts[i].decode("utf-8", "surrogateescape")] = "D "
            i += 1
    return out


def _index_digest(repo: str) -> str:
    p = _git(repo, "ls-files", "--stage", "-z", binary=True)
    return hashlib.sha1(p.stdout or b"", usedforsecurity=False).hexdigest()


def _index_changed(snap) -> bool:
    return (_sig(snap.index) != snap.index_sig
            and _index_digest(snap.repo) != snap.index_digest)


def _cache_dir(ws) -> str:
    return os.path.join(ws.run_dir, "net-copies")


def _quarantine_dir(ws) -> str:
    # Outside run_dir: close() removes run_dir, and quarantined files are
    # the user's to look at.
    from aiforge_core.runtime.team_workspace import _runs_root
    return os.path.join(_runs_root(), "quarantine",
                        os.path.basename(ws.run_dir))


def _copy_of(ws, repo: str, rel: str) -> str | None:
    """A copy of ``repo/rel`` in the run's cache, refreshed only when the
    file changed since the last copy."""
    src = os.path.join(repo, rel)
    if not os.path.lexists(src) or os.path.isdir(src) and not os.path.islink(src):
        return None
    sig = _sig(src)
    if not os.path.islink(src) and sig and sig[1] > _MAX_COPY:
        return None
    key = hashlib.sha1(rel.encode("utf-8", "surrogateescape"),
                       usedforsecurity=False).hexdigest()
    dst = os.path.join(_cache_dir(ws), key)
    cache = ws.__dict__.setdefault("_net_cache", {})
    if cache.get(rel) == sig and os.path.lexists(dst):
        return dst
    os.makedirs(_cache_dir(ws), exist_ok=True)
    if os.path.lexists(dst):
        os.remove(dst)
    shutil.copy2(src, dst, follow_symlinks=False)
    cache[rel] = sig
    return dst


# ── the net ────────────────────────────────────────────────────────────────

def _applies(cwd, name: str, session_id):
    from aiforge_core.runtime import shell_writes, team_workspace
    from aiforge_core.runtime.team_run_life import fold
    ws = team_workspace.for_cwd(cwd) if cwd else None
    if ws is None:
        return None
    if name not in shell_writes.SHELL_TOOLS and name not in _NET_TOOLS_EXTRA:
        return None
    if session_id is not None:
        from aiforge_core.runtime import chat_write_grants
        if any(fold(g) == fold(ws.repo)
               for g in chat_write_grants.granted(session_id)):
            return None
    return ws


def snapshot(ws) -> Snap:
    repo = ws.repo
    status = _status(repo)
    entries = {}
    for rel, xy in _parse(status).items():
        path = os.path.join(repo, rel)
        entries[rel] = (xy, _sig(path), _copy_of(ws, repo, rel))
    index = _out(repo, "rev-parse", "--git-path", "index")
    index = index if os.path.isabs(index) else os.path.join(repo, index)
    index_copy = ""
    if os.path.isfile(index):
        os.makedirs(_cache_dir(ws), exist_ok=True)
        index_copy = os.path.join(_cache_dir(ws), "index")
        cache = ws.__dict__.setdefault("_net_cache", {})
        if cache.get("\0index") != _sig(index) or not os.path.exists(index_copy):
            shutil.copy2(index, index_copy)
            cache["\0index"] = _sig(index)
    ref = _out(repo, "symbolic-ref", "-q", "HEAD")
    return Snap(repo=repo, head=_out(repo, "rev-parse", "HEAD"), ref=ref,
                ref_sha=_out(repo, "rev-parse", ref) if ref else "",
                status=status, entries=entries, index=index,
                index_sig=_sig(index), index_copy=index_copy,
                index_digest=_index_digest(repo))


def _unchanged(snap: Snap) -> bool:
    repo = snap.repo
    if _out(repo, "symbolic-ref", "-q", "HEAD") != snap.ref:
        return False
    if _out(repo, "rev-parse", "HEAD") != snap.head:
        return False
    if _index_changed(snap):
        return False
    if _status(repo) != snap.status:
        return False
    return all(_sig(os.path.join(repo, rel)) == sig
               for rel, (_xy, sig, _c) in snap.entries.items())


def begin(cwd, name: str, session_id=None):
    """The handle :func:`end` needs, or None when the net does not apply."""
    try:
        ws = _applies(cwd, name, session_id)
        if ws is None:
            return None
        from aiforge_core.runtime import cmd_jobs
        with cmd_jobs._LOCK:
            jobs = set(cmd_jobs._JOBS)
        return {"ws": ws, "snap": snapshot(ws), "t0": time.time(),
                "jobs": jobs}
    except Exception as exc:  # noqa: BLE001 — the net never blocks a call
        log.warning("team repo snapshot failed: %s", exc)
        return None


def end(handle, result):
    """``(result, note)`` after the call: restored + a refusal result when
    the command changed the user's checkout. ``note`` is the chat line."""
    if handle is None:
        return result, ""
    ws, snap, t0 = handle["ws"], handle["snap"], handle["t0"]
    try:
        _adopt_new_jobs(ws, snap, t0, handle["jobs"])
        report = restore(ws, snap, t0, time.time())
        report = _merge(report, check_pending(ws, skip=snap))
    except Exception as exc:  # noqa: BLE001
        log.warning("team repo check failed: %s", exc)
        return result, ""
    if not report.get("reverted"):
        return result, _outside_note(report)
    return _refusal(ws, report, result), note_for(ws, report)


def _adopt_new_jobs(ws, snap, t0, before) -> None:
    from aiforge_core.runtime import cmd_jobs
    with cmd_jobs._LOCK:
        new = [j for k, j in cmd_jobs._JOBS.items() if k not in before]
    live = [j for j in new if j.alive()]
    if live:
        with _LOCK:
            _PENDING.setdefault(ws.cwd, []).extend((j, snap, t0) for j in live)


def check_pending(ws, skip=None, final: bool = False) -> dict:
    """Re-check the repo for jobs a command left running (see module doc).
    A job that changed it is killed, then the repo restored. ``final``: the
    run ends — every job is settled."""
    with _LOCK:
        items = list(_PENDING.get(ws.cwd, ()))
    report: dict = {}
    keep = []
    for job, snap, t0 in items:
        alive = job.alive()
        if snap is not skip and not _unchanged(snap):
            if alive:
                job.kill("stopped: it changed the user's checkout")
                alive = False
            report = _merge(report, restore(ws, snap, t0, time.time()))
        if alive and not final:
            keep.append((job, snap, t0))
    with _LOCK:
        if keep:
            _PENDING[ws.cwd] = keep
        else:
            _PENDING.pop(ws.cwd, None)
    return report


def _merge(a: dict, b: dict) -> dict:
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, [])
        out[k] += [x for x in v if x not in out[k]]
    return out


# ── restore ────────────────────────────────────────────────────────────────

def _in_window(path: str, t0: float, t1: float) -> bool:
    try:
        m = os.lstat(path).st_mtime
    except OSError:
        # Gone: judge by the folder it was in.
        try:
            m = os.stat(os.path.dirname(path)).st_mtime
        except OSError:
            return True
    return t0 - _SLACK_S <= m <= t1 + _SLACK_S


def _ref_moved_in_window(repo: str, ref: str, t0: float, t1: float) -> bool:
    ts = _out(repo, "reflog", "show", "-n1", "--format=%ct", ref)
    if not ts.isdigit():
        return True
    return t0 - _SLACK_S - 1 <= int(ts) <= t1 + _SLACK_S + 1


def restore(ws, snap: Snap, t0: float, t1: float) -> dict:
    """Put the repo back to ``snap`` for changes made in ``[t0, t1]``.
    ``{reverted, quarantined, outside}``."""
    if _unchanged(snap):
        return {}
    repo = snap.repo
    rep: dict = {"reverted": [], "quarantined": [], "outside": []}
    # 1. HEAD / branch
    cur_ref = _out(repo, "symbolic-ref", "-q", "HEAD")
    if snap.ref and snap.ref_sha and _out(repo, "rev-parse", snap.ref) != snap.ref_sha:
        if _ref_moved_in_window(repo, snap.ref, t0, t1):
            _git(repo, "update-ref", "-m", "aiforge: undo a team-run command",
                 snap.ref, snap.ref_sha)
            rep["reverted"].append(f"{snap.ref} (reset to {snap.ref_sha[:10]})")
        else:
            rep["outside"].append(snap.ref)
    if cur_ref != snap.ref:
        if snap.ref:
            _git(repo, "symbolic-ref", "HEAD", snap.ref)
        else:
            _git(repo, "update-ref", "--no-deref", "HEAD", snap.head)
        rep["reverted"].append("HEAD")
    elif not snap.ref and _out(repo, "rev-parse", "HEAD") != snap.head:
        _git(repo, "update-ref", "--no-deref", "HEAD", snap.head)
        rep["reverted"].append("HEAD")
    # 2. the index
    if snap.index_copy and _index_changed(snap) and (
            _in_window(snap.index, t0, t1) or rep["reverted"]):
        shutil.copy2(snap.index_copy, snap.index)
        rep["reverted"].append("index")
    # 3. files
    now = _parse(_status(repo))
    for rel in sorted(set(now) | set(snap.entries)):
        _restore_path(ws, snap, rel, now.get(rel), t0, t1, rep)
    return {k: v for k, v in rep.items() if v}


def _restore_path(ws, snap, rel, now_xy, t0, t1, rep) -> None:
    repo = snap.repo
    path = os.path.join(repo, rel)
    before = snap.entries.get(rel)
    if before is not None:
        xy, sig, copy = before
        if _sig(path) == sig and now_xy == xy:
            return
        if not _in_window(path, t0, t1):
            rep["outside"].append(rel)
            return
        if copy:                                  # the user's own content
            _put(copy, path)
            rep["reverted"].append(rel)
        elif os.path.lexists(path):               # absent before (a deletion)
            _quarantine(ws, rel, path, rep)
        return
    # clean (tracked, as in HEAD) or absent before
    if not _in_window(path, t0, t1):
        rep["outside"].append(rel)
        return
    in_head = _git(repo, "cat-file", "-e", f"{snap.head}:{rel}").returncode == 0
    if in_head:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        _git(repo, "checkout", snap.head, "--", rel)
        rep["reverted"].append(rel)
    elif os.path.lexists(path):
        _quarantine(ws, rel, path, rep)


def _put(copy: str, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    shutil.copy2(copy, path, follow_symlinks=False)


def _quarantine(ws, rel: str, path: str, rep: dict) -> None:
    dst = os.path.join(_quarantine_dir(ws), rel)
    base, n = dst, 1
    while os.path.lexists(dst):
        dst, n = f"{base}.{n}", n + 1
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(path, dst)
    rep["quarantined"].append(f"{rel} → {dst}")
    rep["reverted"].append(rel)
    # an emptied folder the command created goes too (not the user's files)
    d = os.path.dirname(path)
    while d.startswith(ws.repo + os.sep):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


# ── what the model and the user are told ──────────────────────────────────

def _refusal(ws, report: dict, result) -> dict:
    out = {"ok": False, "error": "changed_users_checkout",
           "reverted": report.get("reverted", []),
           "hint": (f"That command changed the user's checkout {ws.repo}; "
                    f"it was put back. Run it in the worktree {ws.cwd} "
                    "(relative paths, or paths under it) — the team run "
                    "must not touch the user's repo.")}
    if report.get("quarantined"):
        out["quarantined"] = report["quarantined"]
    if report.get("outside"):
        out["changed_outside_the_command"] = report["outside"]
    if isinstance(result, dict):
        out["command_result"] = {k: result.get(k) for k in
                                 ("ok", "code", "output", "error", "id")
                                 if k in result}
    return out


def note_for(ws, report: dict) -> str:
    if not report.get("reverted"):
        return _outside_note(report)
    text = (f"A team-run command changed your checkout `{ws.repo}` — put "
            "back: " + ", ".join(f"`{r}`" for r in report["reverted"][:8]))
    if report.get("quarantined"):
        text += (f". New files it made were moved to "
                 f"`{_quarantine_dir(ws)}`")
    extra = _outside_note(report)
    return text + "." + (" " + extra if extra else "")


def _outside_note(report: dict) -> str:
    if not report.get("outside"):
        return ""
    return ("Changed outside the command (your own edits?), left alone: "
            + ", ".join(f"`{o}`" for o in report["outside"][:8]) + ".")


def poll(cwd) -> str:
    """After any tool call of a team run: re-check jobs a command left
    running. Returns the chat note (empty when nothing changed)."""
    try:
        from aiforge_core.runtime import team_workspace
        ws = team_workspace.for_cwd(cwd) if cwd else None
        with _LOCK:
            if ws is None or not _PENDING.get(ws.cwd):
                return ""
        report = check_pending(ws)
    except Exception as exc:  # noqa: BLE001
        log.warning("team repo poll failed: %s", exc)
        return ""
    return note_for(ws, report) if report else ""


def settle(ws) -> str:
    """The run closes: settle every job it left running. Returns a note."""
    try:
        report = check_pending(ws, final=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("team repo settle failed: %s", exc)
        return ""
    return note_for(ws, report) if report else ""


__all__ = ["begin", "check_pending", "end", "note_for", "poll", "restore",
           "settle", "snapshot"]
