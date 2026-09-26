"""The safety net under a team run's shell commands: if a command changed the
user's REAL checkout, the run stops and says so. Nothing is ever written.

A team run works in its own worktree, and team_repo_guard refuses the obvious
commands that write into the user's repo. A text check cannot be complete
(``env -C <repo>``, ``cd "$(…)"``, ``find <repo> | xargs sed -i``, a heredoc
``python3 - <<PY open('<repo>/x','w')``, ``cp -t <repo>`` …), so every shell
command of a team run is bracketed:

* :func:`begin` — before it runs: HEAD, the branch ref, ``git status -z``
  and a content hash of every dirty path (no copies — cheap);
* :func:`end` — after it: the same again. If the checkout changed (the ref
  moved, or a path changed / appeared / went away) NOTHING is reverted — an
  automatic undo cannot tell the command's change from the user's own edit,
  and undoing the wrong one loses the user's work. The tool result becomes
  ``{ok: false, error: "changed_users_checkout", changed, ref_moved}``, a
  chat line lists the paths, and the run is PAUSED for the user
  (:func:`alert_for`; team route ``watch`` stops the turn and asks).

A job a command left running is checked again on every later tool call of
the run and when it closes. If a snapshot cannot be taken (a git error or
timeout, ``index.lock`` present) the check is skipped for that call with a
visible warning — a failed read is never taken as "nothing there".
Only in a team run (team_workspace.for_cwd), and not when the user granted
the real repo. ``AIFORGE_TEAM_REPO_NET=off`` turns it off.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

_NET_TOOLS_EXTRA = frozenset({"command_wait", "command_output", "command_kill"})
_HASH_MAX = 16 * 1024 * 1024          # bigger files: size + mtime, not hashed
_LOCK = threading.Lock()
_PENDING: dict[str, list] = {}          # ws.cwd → [(job, snap)]
_ALERTS: dict[str, dict] = {}           # ws.cwd → {changed, ref_moved}


class SnapError(RuntimeError):
    """The checkout could not be read reliably — skip, never assume empty."""


@dataclass
class Snap:
    repo: str
    head: str
    ref: str
    ref_sha: str
    status: bytes
    marks: dict                  # path → content mark


def _off() -> bool:
    return (os.environ.get("AIFORGE_TEAM_REPO_NET", "detect") or "detect"
            ).strip().lower() in ("off", "0", "false", "no")


def _git(repo: str, *args: str) -> bytes:
    """stdout of a read-only git call; SnapError on any failure."""
    try:
        p = subprocess.run(["git", "--no-optional-locks", "-C", repo, *args],
                           capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 — timeout, missing git, …
        raise SnapError(f"git {args[0]}: {exc}") from exc
    if p.returncode != 0:
        raise SnapError(f"git {args[0]} failed: "
                        f"{(p.stderr or b'').decode(errors='replace')[:200]}")
    return p.stdout or b""


def _parse(status: bytes) -> list[str]:
    out: list[str] = []
    parts = status.split(b"\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        out.append(rec[3:].decode("utf-8", "surrogateescape"))
        if rec[:1] in (b"R", b"C") and i < len(parts):
            out.append(parts[i].decode("utf-8", "surrogateescape"))
            i += 1
    return out


def _mark(path: str) -> str:
    """What a path holds now: a content hash, a size+mtime for a big file,
    ``dir`` for a folder / submodule, ``-`` when missing."""
    try:
        st = os.lstat(path)
    except OSError:
        return "-"
    if os.path.isdir(path) and not os.path.islink(path):
        return "dir"
    if os.path.islink(path):
        return "link:" + os.readlink(path)
    if st.st_size > _HASH_MAX:
        return f"big:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha1(usedforsecurity=False)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        raise SnapError(f"read {path}: {exc}") from exc
    return h.hexdigest()


def snapshot(repo: str) -> Snap:
    gitdir = _git(repo, "rev-parse", "--absolute-git-dir").decode().strip()
    if os.path.exists(os.path.join(gitdir, "index.lock")):
        raise SnapError("index.lock present (another git command is running)")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    try:
        ref = _git(repo, "symbolic-ref", "-q", "HEAD").decode().strip()
    except SnapError:
        ref = ""                                   # detached HEAD
    ref_sha = _git(repo, "rev-parse", ref).decode().strip() if ref else ""
    status = _git(repo, "status", "--porcelain=v1", "-z",
                  "--untracked-files=normal")
    marks = {rel: _mark(os.path.join(repo, rel)) for rel in _parse(status)}
    return Snap(repo, head, ref, ref_sha, status, marks)


def diff(a: Snap, b: Snap) -> tuple[list[str], bool]:
    """``(changed paths, ref moved)`` between two snapshots."""
    ref_moved = (a.head, a.ref, a.ref_sha) != (b.head, b.ref, b.ref_sha)
    changed = sorted(p for p in set(a.marks) | set(b.marks)
                     if a.marks.get(p) != b.marks.get(p))
    if not changed and a.status != b.status:
        changed = ["(git status changed)"]
    return changed, ref_moved


# ── the net ────────────────────────────────────────────────────────────────

def _ws_for(cwd, name: str, session_id):
    from aiforge_core.runtime import shell_writes, team_workspace
    from aiforge_core.runtime.team_run_life import fold
    if _off():
        return None
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


def begin(cwd, name: str, session_id=None):
    """The handle :func:`end` needs; None when the net does not apply."""
    ws = _ws_for(cwd, name, session_id)
    if ws is None:
        return None
    from aiforge_core.runtime import cmd_jobs
    with cmd_jobs._LOCK:
        jobs = set(cmd_jobs._JOBS)
    try:
        snap = snapshot(ws.repo)
    except SnapError as exc:
        return {"ws": ws, "snap": None, "warn": str(exc), "jobs": jobs}
    return {"ws": ws, "snap": snap, "warn": "", "jobs": jobs}


def end(handle, result):
    """``(result, note)`` after the call. See module doc."""
    if handle is None:
        return result, ""
    ws, snap = handle["ws"], handle["snap"]
    if snap is None:
        return result, _warn_note(ws, handle["warn"])
    _adopt_new_jobs(ws, snap, handle["jobs"])
    try:
        now = snapshot(ws.repo)
    except SnapError as exc:
        return result, _warn_note(ws, str(exc))
    changed, moved = diff(snap, now)
    pending_note = check_pending(ws, skip=snap)
    if not changed and not moved:
        return result, pending_note
    _raise_alert(ws, changed, moved)
    return _refusal(ws, changed, moved, result), note_for(ws, changed, moved)


def _adopt_new_jobs(ws, snap, before) -> None:
    from aiforge_core.runtime import cmd_jobs
    with cmd_jobs._LOCK:
        new = [j for k, j in cmd_jobs._JOBS.items() if k not in before]
    live = [j for j in new if j.alive()]
    if live:
        with _LOCK:
            _PENDING.setdefault(ws.cwd, []).extend((j, snap) for j in live)


def check_pending(ws, skip=None, final: bool = False) -> str:
    """Compare the checkout with the snapshot taken before each job a command
    left running. A change is reported and pauses the run; nothing is
    reverted, and no job is killed here (pausing ends the turn, which stops
    the turn's handed-off jobs as always). Returns the chat note."""
    with _LOCK:
        items = list(_PENDING.get(ws.cwd, ()))
    if not items:
        return ""
    keep, note = [], ""
    try:
        now = snapshot(ws.repo)
    except SnapError as exc:
        return _warn_note(ws, str(exc))
    for job, snap in items:
        if snap is not skip:
            changed, moved = diff(snap, now)
            if (changed or moved) and not note:
                _raise_alert(ws, changed, moved)
                note = note_for(ws, changed, moved)
        if job.alive() and not final and not note:
            keep.append((job, snap))
    with _LOCK:
        if keep:
            _PENDING[ws.cwd] = keep
        else:
            _PENDING.pop(ws.cwd, None)
    return note


def poll(cwd) -> str:
    """After any tool call of a team run: re-check jobs a command left
    running. Returns the chat note."""
    from aiforge_core.runtime import team_workspace
    ws = team_workspace.for_cwd(cwd) if cwd else None
    if ws is None:
        return ""
    with _LOCK:
        if not _PENDING.get(ws.cwd):
            return ""
    return check_pending(ws)


def settle(ws) -> str:
    """The run closes: a last look for jobs it left running."""
    try:
        return check_pending(ws, final=True)
    finally:
        with _LOCK:
            _PENDING.pop(ws.cwd, None)
            _ALERTS.pop(ws.cwd, None)


# ── pausing the run ────────────────────────────────────────────────────────

def _raise_alert(ws, changed, moved) -> None:
    with _LOCK:
        cur = _ALERTS.setdefault(ws.cwd, {"changed": [], "ref_moved": False})
        cur["changed"] += [c for c in changed if c not in cur["changed"]]
        cur["ref_moved"] = cur["ref_moved"] or moved


def alert_for(cwd) -> dict | None:
    """The pending "your checkout changed" alert of the run owning ``cwd``."""
    from aiforge_core.runtime import team_workspace
    ws = team_workspace.for_cwd(cwd) if cwd else None
    if ws is None:
        return None
    with _LOCK:
        a = _ALERTS.get(ws.cwd)
        return dict(a) if a else None


def take_alert(ws) -> dict | None:
    with _LOCK:
        return _ALERTS.pop(ws.cwd, None)


def pause_text(ws, alert: dict) -> str:
    paths = _listed(alert.get("changed") or [])
    if alert.get("ref_moved"):
        paths = (paths + ", " if paths else "") + "HEAD / the branch"
    return (f"A command in this team run changed your checkout ({paths}). "
            f"Nothing was reverted. Check `git status` in `{ws.repo}`. "
            "Reply **continue** to go on.")


# ── what the model and the user are told ──────────────────────────────────

def _listed(paths) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:8])
    return shown + (f" and {len(paths) - 8} more" if len(paths) > 8 else "")


def _refusal(ws, changed, moved, result) -> dict:
    out = {"ok": False, "error": "changed_users_checkout", "changed": changed,
           "ref_moved": moved,
           "hint": (f"That command changed the user's checkout {ws.repo} — "
                    "nothing was reverted and the run is paused for the "
                    f"user. Work only in the worktree {ws.cwd}.")}
    if isinstance(result, dict):
        out["command_result"] = {k: result.get(k) for k in
                                 ("ok", "code", "returncode", "output",
                                  "stdout", "stderr", "error", "id")
                                 if k in result}
    return out


def note_for(ws, changed, moved) -> str:
    what = _listed(changed) if changed else ""
    if moved:
        what = (what + "; " if what else "") + "HEAD / the branch moved"
    return (f"A team-run command changed your checkout `{ws.repo}`: {what}. "
            "Nothing was reverted.")


def _warn_note(ws, why: str) -> str:
    log.warning("team repo check skipped for %s: %s", ws.repo, why)
    return (f"Could not check your checkout `{ws.repo}` around that command "
            f"({why}); it was not watched this time.")


__all__ = ["SnapError", "alert_for", "begin", "check_pending", "diff", "end",
           "note_for", "pause_text", "poll", "settle", "snapshot",
           "take_alert"]
