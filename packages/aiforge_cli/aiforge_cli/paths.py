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
import sys

BOX_PREFIX = "/host"


def _is_windows(platform: str | None = None) -> bool:
    return (platform or os.name) in ("nt", "windows")


def normalize_host(path: str, *, home: str | None = None, cwd: str | None = None,
                   platform: str | None = None) -> str:
    """Absolute, collapsed, no trailing separator, ``~`` expanded.

    ``..`` is collapsed HERE, before anything is compared: without it
    ``~/work/../.ssh`` slipped past every guard as a string while the
    filesystem cheerfully resolved it. Symlinks are left alone — the user
    mounts the path they typed and that is the path they see inside the box —
    so the guards resolve them separately (see :func:`mount_refusal`).
    """
    home = os.path.expanduser("~") if home is None else home
    if path.startswith("~"):
        path = home + path[1:]
    win = _is_windows(platform)
    if win:
        path = path.replace("/", "\\")
        # `C:work` is drive-RELATIVE on Windows, not absolute: it resolves
        # against that drive's own working directory, which is not something to
        # hand a container.
        absolute = len(path) > 2 and path[1] == ":" and path[2] == "\\"
    else:
        absolute = path.startswith("/")
    if not absolute:
        path = os.path.join(cwd if cwd is not None else os.getcwd(), path)
    sep = "\\" if win else "/"
    path = _normpath(path, win)
    while len(path) > 1 and path.endswith(sep) and not path.endswith(":" + sep):
        path = path[:-1]
    return path


def _normpath(path: str, win: bool) -> str:
    """Collapse ``.``, ``..`` and duplicate separators, for either flavour."""
    if win:
        drive, sep, rest = path.partition(":")
        if not sep:
            # No drive letter (a UNC share, say). Partitioning would have put
            # the whole path in `drive` and normalised "" to ".", producing
            # `\\nas\dev:.` — a path that exists nowhere.
            return posixpath.normpath(path.replace("\\", "/")).replace("/", "\\")
        collapsed = posixpath.normpath(rest.replace("\\", "/"))
        return f"{drive}:" + collapsed.replace("/", "\\")
    return posixpath.normpath(path)


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


def _case_insensitive(platform: str | None = None) -> bool:
    """Whether path comparison must fold case.

    Windows always, and macOS by default — APFS is case-INsensitive, so
    ``~/.SSH`` and ``~/.ssh`` are the same directory and a case-sensitive
    guard let the second one through under the first one's name.
    """
    if _is_windows(platform):
        return True
    if platform is None:
        return sys.platform == "darwin"
    return platform == "darwin"


def _within(child: str, parent: str, *, platform: str | None = None) -> bool:
    sep = "\\" if _is_windows(platform) else "/"
    if _case_insensitive(platform):
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


# Folders that must never be handed to the agent, relative to home. The first
# is the load-bearing one: ~/.config/aiforge holds approved-mounts, the host's
# answer to the box's mount requests. Mounting it (or any ancestor) would let
# the box approve its own future mounts — a one-way door out of the sandbox.
# The rest hold credentials that would turn a mount into a key handover.
GUARDED_HOME_DIRS = (".config", ".ssh", ".gnupg", ".aws", ".kube", ".docker",
                     ".gitconfig", ".netrc", ".npmrc", ".pypirc")
GUARDED_ABSOLUTE = ("/etc", "/var/run", "/run", "/proc", "/sys", "/boot", "/dev")


def _resolve(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _guarded(path: str, home: str, *, platform: str | None = None,
             extra_guards: tuple[str, ...] = ()) -> str | None:
    """The reason this folder is off limits, or None.

    Checked in BOTH directions — a guard inside the candidate is as bad as the
    candidate inside a guard, because docker binds the whole tree either way —
    and against the resolved path as well as the typed one, so a symlink is not
    a way around the list.
    """
    # The approvals file's own directory, wherever XDG_CONFIG_HOME puts it. A
    # literal "~/.config" guard missed the supported case of an XDG home
    # elsewhere, and write access to approved-mounts is the one-way door: the
    # box could then approve every line it appends to mounts.list itself.
    for guard in extra_guards:
        if _touches(path, guard, platform=platform):
            return ("holds this host's mount approvals — mounting it would let "
                    "the sandbox approve its own mounts")
    for name in GUARDED_HOME_DIRS:
        if _touches(path, os.path.join(home, name), platform=platform):
            if name == ".config":
                return ("holds the host's mount approvals (~/.config/aiforge) — "
                        "mounting it would let the sandbox approve its own mounts")
            return f"holds credentials (~/{name})"
    if not _is_windows(platform):
        for guard in GUARDED_ABSOLUTE:
            if _touches(path, guard, platform=platform):
                return f"is inside {guard} — system files, not a project"
    return None


def _touches(path: str, guard: str, *, platform: str | None = None) -> bool:
    """Whether a bind of ``path`` would expose ``guard``, or vice versa.

    Both sides are canonicalised, not just the candidate: on macOS /etc IS
    /private/etc, and a home that is itself a symlink made every ~/… guard
    unmatchable under the resolved name.
    """
    for candidate in _candidates(path):
        for target in {guard, _resolve(guard)}:
            if (_within(candidate, target, platform=platform)
                    or _within(target, candidate, platform=platform)):
                return True
    return False


def _touches_home(path: str, home: str, *, platform: str | None = None) -> bool:
    """Home itself, or anything containing it."""
    for candidate in _candidates(path):
        for target in {home, _resolve(home)}:
            if _within(target, candidate, platform=platform):
                return True
    return False


def _candidates(path: str) -> tuple[str, ...]:
    """The typed path and where it actually leads.

    docker resolves a symlinked bind at the host, so `ln -s ~/.ssh keys` would
    otherwise mount the keys under a name no guard recognises.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return (path,)
    return (path,) if real == path else (path, real)


def mount_refusal(path: str, *, home: str | None = None, platform: str | None = None,
                  extra_guards: tuple[str, ...] = ()) -> str | None:
    """Why this folder must not be mounted, or None if it may be.

    Mirrors run.sh's own checks so both entry points refuse the same things.
    Mounting home (or anything above it) would hand the agent every dotfile,
    every key and the approvals file itself, which is the one thing the box must
    not be able to edit.
    """
    home = os.path.expanduser("~") if home is None else home
    if not path:
        return "is empty"
    win = _is_windows(platform)
    # A mount line becomes `"<host>:<box>"` in a compose file, so a path
    # carrying the separator (or a shell/compose metacharacter) would break the
    # file rather than mount the folder. Windows paths legitimately contain a
    # drive colon and backslashes, so only the rest is refused there.
    bad = '#"$' if win else ':#"\\$'
    if any(c in path for c in bad) or (not win and ":" in path):
        return f"needs a path without {' '.join(bad)}"
    # A newline would split the single-quoted compose scalar across lines and
    # the file would not parse — a folder nobody can mount is better told so.
    if any(c in path for c in "\n\r\t"):
        return "needs a path without a newline or tab"
    if win:
        # `C:work` is drive-RELATIVE: it resolves against that drive's own
        # working directory, so it names different folders at different times.
        if len(path) < 3 or path[1] != ":" or path[2] != "\\" or ":" in path[2:]:
            return "needs an absolute path"
        if len(path.rstrip("\\")) <= 2:
            return "is a whole drive — too broad"
    else:
        if not path.startswith("/"):
            return "needs an absolute path"
        if path == "/":
            return "is the whole filesystem — too broad"
    if _touches_home(path, home, platform=platform):
        return "is your home folder (or above it) — too broad"
    guard = _guarded(path, home, platform=platform, extra_guards=extra_guards)
    if guard is not None:
        return guard
    if not os.path.isdir(path):
        return "is not a folder on this machine"
    return None
