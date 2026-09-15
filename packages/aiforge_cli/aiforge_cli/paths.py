"""Host paths in, box paths out.

On macOS and Linux a mounted folder appears inside the sandbox at the same
absolute path, so the mapping is the identity and nothing here fires. Windows
has no such path to share: ``C:\\Users\\x\\work`` is mounted at
``/host/c/Users/x/work``, and every path handed to the API (a session cwd, a
file the user referenced) has to be translated — and translated back before it
is shown to anyone.
"""

from __future__ import annotations

import os
import posixpath

BOX_PREFIX = "/host"


def _is_windows(platform: str | None = None) -> bool:
    return (platform or os.name) in ("nt", "windows")


def normalize_host(path: str, *, home: str | None = None, cwd: str | None = None,
                   platform: str | None = None) -> str:
    """Absolute, no trailing separator, ``~`` expanded — as typed otherwise.

    Symlinks are deliberately NOT resolved: the user mounts the path they said,
    and that is the path they will see inside the box.
    """
    home = os.path.expanduser("~") if home is None else home
    if path.startswith("~"):
        path = home + path[1:]
    win = _is_windows(platform)
    if win:
        path = path.replace("/", "\\")
        absolute = len(path) > 1 and path[1] == ":"
    else:
        absolute = path.startswith("/")
    if not absolute:
        path = os.path.join(cwd if cwd is not None else os.getcwd(), path)
    sep = "\\" if win else "/"
    while len(path) > 1 and path.endswith(sep) and not path.endswith(":" + sep):
        path = path[:-1]
    return path


def to_box(path: str, *, platform: str | None = None) -> str:
    """The path this host folder has INSIDE the sandbox."""
    if not _is_windows(platform):
        return path
    drive, _, rest = path.partition(":")
    rest = rest.replace("\\", "/").lstrip("/")
    return posixpath.join(BOX_PREFIX, drive.lower(), rest) if drive else path


def to_host(path: str, *, platform: str | None = None) -> str:
    """Inverse of :func:`to_box` — what to print for a box path."""
    if not _is_windows(platform):
        return path
    if not path.startswith(BOX_PREFIX + "/"):
        return path
    rest = path[len(BOX_PREFIX) + 1:]
    drive, _, tail = rest.partition("/")
    return f"{drive.upper()}:\\" + tail.replace("/", "\\")


def _within(child: str, parent: str, *, platform: str | None = None) -> bool:
    sep = "\\" if _is_windows(platform) else "/"
    if _is_windows(platform):
        child, parent = child.lower(), parent.lower()
    if child == parent:
        return True
    return child.startswith(parent.rstrip(sep) + sep)


def covering_mount(path: str, mounts: list[str], *, platform: str | None = None) -> str | None:
    """The mounted folder that contains ``path``, or None.

    Returns the LONGEST match, so a nested mount wins over its parent and the
    path we report is the one the box actually resolves.
    """
    hits = [m for m in mounts if _within(path, m, platform=platform)]
    return max(hits, key=len) if hits else None


def mount_refusal(path: str, *, home: str | None = None, platform: str | None = None) -> str | None:
    """Why this folder must not be mounted, or None if it may be.

    Mirrors run.sh's own checks so both entry points refuse the same things.
    Mounting home (or anything above it) would hand the agent every dotfile,
    every key and the approvals file itself, which is the one thing the box must
    not be able to edit.
    """
    home = os.path.expanduser("~") if home is None else home
    win = _is_windows(platform)
    # A mount line becomes `"<host>:<box>"` in a compose file, so a path
    # carrying the separator (or a shell/compose metacharacter) would break the
    # file rather than mount the folder. Windows paths legitimately contain a
    # drive colon and backslashes, so only the rest is refused there.
    bad = '#"$' if win else ':#"\\$'
    if any(c in path for c in bad) or (not win and ":" in path):
        return f"needs a path without {' '.join(bad)}"
    if win:
        if len(path) < 3 or path[1] != ":" or ":" in path[2:]:
            return "needs an absolute path"
        if len(path.rstrip("\\")) <= 2:
            return "is a whole drive — too broad"
    else:
        if not path.startswith("/"):
            return "needs an absolute path"
        if path == "/":
            return "is the whole filesystem — too broad"
    if _within(home, path, platform=platform):
        return "is your home folder (or above it) — too broad"
    if not os.path.isdir(path):
        return "is not a folder on this machine"
    return None
