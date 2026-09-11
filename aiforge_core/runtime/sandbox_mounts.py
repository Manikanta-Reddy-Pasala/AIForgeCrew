"""Host folders the docker-mode sandbox can see — what is mounted, and what the
user asked for.

The box sees ~/.aiforge and nothing else of the host, plus a projects folder
(``--repos``) and any folder the user adds. A running container cannot mount a
new folder into itself: the host's ``./run.sh`` does that when it (re)starts
the box, reading ``~/.aiforge/mounts.list``. So adding a folder here (Settings,
or the chat's ``mount_folder`` tool) records the request, and the answer says
plainly that it lands on the next ``./run.sh`` — and only once the HOST approves
it there (``--mount`` or a prompt): the list lives in ~/.aiforge, which the box
itself can write, so an entry here is a request, never a grant. ``AIFORGE_MOUNTS`` is the list
run.sh actually mounted at start, so "mounted" and "waiting for a restart" are
both real, not guessed.
"""
from __future__ import annotations

import os
import threading

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()
MOUNTS_FILE = "mounts.list"


def _path() -> str:
    return os.path.join(str(config_dir()), MOUNTS_FILE)


def in_sandbox() -> bool:
    return os.environ.get("AIFORGE_SANDBOX", "") == "1"


def mounted() -> list[str]:
    """Folders mounted into this box when it started (run.sh's list)."""
    raw = os.environ.get("AIFORGE_MOUNTS", "")
    return [p for p in raw.split(":") if p]


def requested() -> list[str]:
    """The folders in mounts.list, in order, comments and blanks dropped."""
    try:
        with open(_path(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        p = line.split("#", 1)[0].strip()
        if p and p not in out:
            out.append(p)
    return out


def _write(paths: list[str]) -> None:
    body = ("# Host folders mounted into the AIForge sandbox (same path inside).\n"
            "# Read by ./run.sh on the host at every start; edit in Settings.\n"
            + "".join(f"{p}\n" for p in paths))
    _atomic.write_text(_path(), body)


def validate(path: str) -> str:
    """The normalised host path, or ValueError. Only the host can tell whether
    the folder exists — run.sh checks that and skips (loudly) one that does not."""
    p = (path or "").strip()
    if p.startswith("~"):
        p = os.path.join(os.environ.get("HOME", ""), p[1:].lstrip("/"))
    if not p.startswith("/"):
        raise ValueError("use an absolute host path, like /home/me/projects")
    bad = sorted({c for c in p if c in ':#"\\$\n'})
    if bad:
        raise ValueError("a mount path cannot contain " + " ".join(repr(c) for c in bad))
    p = os.path.normpath(p)
    home = os.path.normpath(os.environ.get("HOME", "") or "/nonexistent")
    if p == "/" or home == p or home.startswith(p.rstrip("/") + "/"):
        raise ValueError("too broad: the whole filesystem or your home folder "
                         "(or above it) defeats the sandbox — mount a project folder")
    return p


def add(path: str) -> list[str]:
    p = validate(path)
    with _LOCK:
        cur = requested()
        if p not in cur:
            cur.append(p)
            _write(cur)
        return cur


def remove(path: str) -> list[str]:
    p = os.path.normpath((path or "").strip())
    with _LOCK:
        cur = [x for x in requested() if os.path.normpath(x) != p]
        _write(cur)
        return cur


def state() -> dict:
    """What Settings shows: every folder with what it is and whether it is
    mounted now, waiting for a restart, or removed but still mounted."""
    live, want = mounted(), requested()
    config_home = live[0] if live else ""
    repos = os.environ.get("AIFORGE_REPO_ROOT", "")
    rows = []
    for p in live:
        if p == config_home:
            rows.append({"path": p, "kind": "config", "status": "mounted"})
        elif p == repos and p not in want:
            rows.append({"path": p, "kind": "projects", "status": "mounted"})
        else:
            rows.append({"path": p, "kind": "folder", "status": "mounted" if p in want
                         else "removed — still mounted until restart"})
    rows += [{"path": p, "kind": "folder",
              "status": "waiting — approve it by running ./run.sh in a terminal on the host"}
             for p in want if p not in live]
    return {"sandbox": in_sandbox(), "folders": rows,
            "restart_needed": any(r["status"] != "mounted" for r in rows),
            "file": _path()}


__all__ = ["mounted", "requested", "add", "remove", "state", "validate"]
