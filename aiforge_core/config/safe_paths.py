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
    return resolved


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
