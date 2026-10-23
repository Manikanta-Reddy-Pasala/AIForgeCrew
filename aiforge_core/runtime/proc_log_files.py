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


def _words(cmd: str) -> list[str]:
    """Shell words with redirect operators as their own tokens. Quotes are
    kept (non-POSIX mode), so a quoted ``'>'`` stays a word, not a redirect
    (``grep '>' file``); :func:`_unquote` strips them from a file name."""
    lex = shlex.shlex(cmd or "", posix=False, punctuation_chars="<>&|;")
    lex.whitespace_split = True
    lex.commenters = ""            # a '#' inside a URL is not a comment
    try:
        return list(lex)
    except ValueError:
        return []


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


def _from_cmd(cmd: str, cwd: str) -> list[str]:
    """Redirect targets and log-looking arguments of a command line."""
    words = _words(cmd)
    found = []
    for i, w in enumerate(words):
        cand = None
        prev = words[i - 1] if i else ""
        if prev in (">", ">>", "&>", "&>>") or (
                prev in (">", ">>") and i >= 2 and words[i - 2].isdigit()):
            cand = w
        elif w.startswith("-") and "=" in w:
            cand = _unquote(w.split("=", 1)[1])
            if not _LOG_NAME.search(cand):
                cand = None
        elif _LOG_NAME.search(_unquote(w)):
            cand = w
        cand = _unquote(cand) if cand else cand
        if not cand or cand.startswith("&") or cand == "/dev/null" \
                or not cand.strip() or cand in "<>&|;":
            continue
        path = os.path.expanduser(cand)
        if not os.path.isabs(path):
            path = os.path.join(cwd or ".", path)
        found.append(os.path.normpath(path))
    return found


def _roots(job) -> list[str]:
    """Where any file a command writes may be shown from: its folder, the
    chat's workspace and the temp folder."""
    import tempfile
    out = []
    for raw in (getattr(job, "cwd", None), _workspace(), tempfile.gettempdir(),
                "/tmp"):
        if raw:
            out.append(os.path.realpath(os.path.expanduser(str(raw))))
    # A root as wide as / or the home folder would show anything at all.
    broad = {"/", os.path.realpath(os.path.expanduser("~"))}
    return [r for r in out if r not in broad]


def _workspace() -> str | None:
    try:
        from aiforge_core.runtime import request_context
        return request_context.get_workspace_dir()
    except Exception:  # noqa: BLE001
        return None


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _showable(real: str, job, follow: str | None) -> bool:
    """A file the dock may show for ``job``: the file show_logs follows (its
    path was already checked), anything inside the job's folder, the
    workspace or the temp folder, or elsewhere a LOG-named file that is not
    inside a hidden folder (~/.ssh, ~/.aws, ~/.config ...)."""
    if real == follow:
        return True
    if _hidden(real):              # ~/.ssh, ~/.aws, ~/.config ... never
        return False
    if any(_under(real, r) for r in _roots(job)):
        return True
    return bool(_LOG_NAME.search(real))


def _hidden(real: str) -> bool:
    """Inside a hidden folder outside the job's own work (a repo's .git is
    covered by _NOT_LOG_NAME)."""
    return any(part.startswith(".") and part not in (".", "..")
               and not part.startswith(".aiforge")
               for part in real.split(os.sep)[:-1])


def _ident(path: str):
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


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
    # Re-check what was listed before FIRST: a file deleted or replaced since
    # is dropped, so a rotated log found again below is listed anew.
    ids: dict = {p: i for p, i in (getattr(job, "log_ids", None) or {}).items()
                 if _ident(p) == i}
    seen = [p for p in seen if p in ids]
    follow_real = os.path.realpath(follow) if follow else None
    for path in found:
        real = os.path.realpath(path)
        if real in own or real in ids or not _ok_file(real) \
                or not _showable(real, job, follow_real):
            continue
        ident = _ident(real)
        if ident is not None:
            ids[real] = ident
    seen = seen + [p for p in ids if p not in seen]
    seen = seen[:MAX_FILES]
    job.log_ids = {p: ids[p] for p in seen}
    job.log_files = seen
    job._log_files_at = now
    return seen


def open_listed(ids: dict, path: str):
    """Open ``path`` for reading when it is a listed file AND still the very
    file that was listed (same device and inode, not a symlink now). Returns
    ``(real path, binary file object)`` or None. The caller closes it."""
    real = os.path.realpath(str(path or ""))
    want = (ids or {}).get(real)
    if want is None:
        return None
    try:
        # O_NONBLOCK: a listed file swapped for a FIFO must not hang the read.
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != tuple(want):
            os.close(fd)
            return None
        return real, os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


__all__ = ["MAX_FILES", "detect", "open_listed"]
