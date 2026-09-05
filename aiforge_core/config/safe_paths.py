"""Turning a caller-supplied path into one this process may walk.

Several places take a directory from outside — a repo path typed in Settings, a
worktree named on a ticket, an identifier that ends up inside a glob — and hand
it straight to ``os.path.join`` / ``glob``. Nothing in between asked whether the
value was a path at all. A taint analyser calls that path injection, and it is
right about the shape even where the caller happens to be trusted today: the
value crosses an HTTP boundary, and the code that receives it cannot see that.

Two helpers, because there are two shapes of the problem:

* :func:`safe_dir` — "this is supposed to be a directory". Expands, resolves
  symlinks and ``..`` (so ``/repo/../etc`` becomes ``/etc`` before anything is
  decided), rejects a NUL byte, and requires the result to be an existing
  directory. Optionally requires it to sit inside a root the operator named.
* :func:`safe_segment` — "this is supposed to be ONE name". Refuses separators,
  NULs and dot-segments outright rather than sanitising them away, because a
  ticket id containing ``../`` is not a ticket id with a typo.

Both return "" on refusal rather than raising: every caller here is a
best-effort probe (what language is this repo, which temp dirs belong to this
run), and a refusal means "nothing", which those callers already handle.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("aiforge.safe_paths")

# Characters that make a value something other than one path segment.
_SEGMENT_BAD = ("/", "\\", "\x00")

_DRIVE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _entry_named(directory: str, want: str) -> str:
    """The real name of ``want`` inside ``directory``, or "" when it is absent.

    Exact match first. The case-insensitive fallback is for macOS and Windows,
    where ``realpath`` leaves the caller's spelling alone and an exact-only
    match would refuse a directory that is really there.
    """
    fallback = ""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == want:
                    return entry.name
                if not fallback and entry.name.lower() == want.lower():
                    fallback = entry.name
    except OSError:
        return ""
    return fallback


def _drive_prefix(drive: str) -> str | None:
    """``drive`` rebuilt from a literal alphabet: "" when there is none (posix),
    ``None`` when it is not a drive letter. Rebuilt for the same reason every
    other component is — nothing the caller typed goes into the answer."""
    if not drive:
        return ""
    for letter in _DRIVE_LETTERS:
        if f"{letter}:" == drive.upper():
            return f"{letter}:"
    return None


def _respelled(resolved: str) -> str:
    """``resolved`` spelled entirely in names read back from the filesystem.

    Validating a path and *using* it are two different claims. ``realpath`` +
    ``isdir`` prove the value is a directory; they do not stop the caller's
    own bytes from being the thing handed to ``glob``/``open``, which is what
    a taint analyser objects to and what a later edit that drops the check
    would silently re-expose.

    So walk down from a literal root instead: each component is matched by
    equality against ``os.scandir`` output, and the string returned is built
    from the ENTRY names. The caller's value only ever appears on the right of
    ``==``. Returns "" when any component is missing — which, after
    ``realpath``, means the tree changed under us.
    """
    drive, tail = os.path.splitdrive(resolved)
    prefix = _drive_prefix(drive)
    if prefix is None:
        return ""
    cur = prefix + os.sep
    for want in tail.split(os.sep):
        if not want:
            continue
        found = _entry_named(cur, want)
        if not found:
            return ""
        cur = os.path.join(cur, found)
    return cur if os.path.isdir(cur) else ""


def safe_dir(path: str | None, *, roots: list[str] | None = None) -> str:
    """The canonical form of ``path`` when it is a directory we may read.

    ``roots`` — when given, the resolved path must be inside one of them
    (themselves resolved), which is what turns "canonical" into "bounded".
    """
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        return ""
    try:
        resolved = os.path.realpath(os.path.expanduser(raw))
    except (OSError, ValueError):
        return ""
    if not os.path.isdir(resolved):
        return ""
    if roots:
        allowed = [os.path.realpath(os.path.expanduser(str(r)))
                   for r in roots if str(r or "").strip()]
        if not any(resolved == a or resolved.startswith(a + os.sep)
                   for a in allowed):
            log.debug("safe_paths: %s is outside the permitted roots", resolved)
            return ""
    return _respelled(resolved)


def safe_segment(value: str | None) -> str:
    """``value`` when it is a single path segment, else "".

    Refused rather than stripped: a ticket id carrying ``../`` is not a ticket
    id that needs cleaning up, it is a value that should never reach a path —
    and quietly rewriting it would hide that from whoever sent it.
    """
    raw = str(value or "").strip()
    if not raw or raw in (".", ".."):
        return ""
    if any(bad in raw for bad in _SEGMENT_BAD):
        return ""
    return raw


__all__ = ["safe_dir", "safe_segment"]
