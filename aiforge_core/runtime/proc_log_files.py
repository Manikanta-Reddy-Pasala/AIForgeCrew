"""The log files a chat's running command writes (or follows).

The Processes dock shows a command's own output, but servers and builds
often write the interesting part to files: ``app.log``, ``build/out.txt``,
``nohup.out``. These are found two ways:

* the files the command's processes have open — written to, or a log file
  read (``tail -F app.log``) — via psutil, or ``/proc/<pid>/fd`` on Linux;
* paths in the command line: a redirect target (``> run.log``, ``2>> err``)
  or an argument that looks like a log file (``--log-file=app.log``).

Only regular files that exist are reported. The list is what the dock may
open for that job, nothing else: :func:`allowed` is the check the file
routes make, so a request cannot read an arbitrary path.
"""
from __future__ import annotations

import os
import re
import shlex
import time

#: Most files reported per job.
MAX_FILES = 20
_CACHE_S = 3.0
_LOG_NAME = re.compile(
    r"(?:\.(?:log|out|err|trace|jsonl)(?:\.\d+)?|(?:^|/)nohup\.out|"
    r"\.log\.[\w-]+)$", re.I)
_REDIRECT = re.compile(r"^(?:\d|&)?>>?$")
_REDIRECT_JOINED = re.compile(r"^(?:\d|&)?>>?(.+)$")
_NOT_LOGS = ("/proc/", "/sys/", "/dev/")
#: Files a process writes that are not logs: databases, locks, caches.
_NOT_LOG_NAME = re.compile(
    r"\.(?:db|sqlite\d?|db-wal|db-shm|db-journal|wal|shm|lock|pid|sock|pyc|"
    r"so|o|a|class|jar|zip|gz|png|jpg|bin|idx|pack)$|/\.git/", re.I)


def _ok_file(path: str) -> bool:
    if not path or path.startswith(_NOT_LOGS) or path.endswith(" (deleted)"):
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _from_cmd(cmd: str, cwd: str) -> list[str]:
    """Redirect targets and log-looking arguments of a command line."""
    try:
        words = shlex.split(cmd or "", posix=True)
    except ValueError:
        words = (cmd or "").split()
    found = []
    after_redirect = False
    for w in words:
        cand = None
        if after_redirect:
            cand, after_redirect = w, False
        elif _REDIRECT.match(w):
            after_redirect = True
            continue
        else:
            m = _REDIRECT_JOINED.match(w)
            if m and not m.group(1).startswith("&"):
                cand = m.group(1)
            elif "=" in w and w.startswith("-"):
                cand = w.split("=", 1)[1]
            else:
                cand = w
            if cand and not (m or _LOG_NAME.search(cand)):
                cand = None
        if not cand or cand.startswith("&") or cand == "/dev/null":
            continue
        path = os.path.expanduser(cand)
        if not os.path.isabs(path):
            path = os.path.join(cwd or ".", path)
        found.append(os.path.normpath(path))
    return found


def _group_pids(job) -> list[int]:
    pid = getattr(job.proc, "pid", None)
    if not isinstance(pid, int):
        return []
    try:
        import psutil
        root = psutil.Process(pid)
        return [pid] + [c.pid for c in root.children(recursive=True)]
    except Exception:  # noqa: BLE001 — psutil missing or the process is gone
        return [pid]


def _open_files(pid: int) -> list[tuple[str, bool]]:
    """``(path, writable)`` for each regular file ``pid`` has open."""
    try:
        import psutil
        out = []
        for f in psutil.Process(pid).open_files():
            mode = getattr(f, "mode", "") or ""
            out.append((f.path, mode not in ("r", "")))
        if out or not os.path.isdir(f"/proc/{pid}/fd"):
            return out
    except Exception:  # noqa: BLE001
        pass
    out = []
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return out
    for fd in fds:
        try:
            path = os.readlink(os.path.join(fd_dir, fd))
            with open(f"/proc/{pid}/fdinfo/{fd}") as fh:
                flags = int(fh.read().split("flags:")[1].split()[0], 8)
        except (OSError, IndexError, ValueError):
            continue
        out.append((path, bool(flags & (os.O_WRONLY | os.O_RDWR))))
    return out


def _spool_paths(job) -> set[str]:
    """The job's own output files (already shown as its output)."""
    out = set()
    for s in getattr(job, "streams", ()):
        if isinstance(s, str):
            out.add(os.path.realpath(s))
    return out


def detect(job) -> list[str]:
    """The log files of a running ``job`` (cached a few seconds), merged with
    every file found for it before, so a log the command closed and reopens
    (``open(..., "a")`` per line) stays listed."""
    now = time.monotonic()
    cached = getattr(job, "_log_files_at", None)
    seen: list[str] = list(getattr(job, "log_files", None) or [])
    if cached is not None and now - cached < _CACHE_S:
        return seen
    found: list[str] = []
    follow = getattr(job, "follow_path", None)
    if follow:
        found.append(follow)
    found += _from_cmd(str(job.cmd), str(getattr(job, "cwd", "") or ""))
    if job.alive():
        for pid in _group_pids(job):
            for path, writable in _open_files(pid):
                if _LOG_NAME.search(path) or (
                        writable and not _NOT_LOG_NAME.search(path)):
                    found.append(path)
    own = _spool_paths(job)
    for path in found:
        real = os.path.realpath(path)
        if real in own or real in seen or not _ok_file(real):
            continue
        seen.append(real)
    seen = seen[:MAX_FILES]
    job.log_files = seen
    job._log_files_at = now
    return seen


def allowed(files, path: str) -> str | None:
    """``path`` when it is one of ``files`` (compared as real paths)."""
    real = os.path.realpath(str(path or ""))
    return real if real in set(files or ()) and _ok_file(real) else None


__all__ = ["MAX_FILES", "allowed", "detect"]
