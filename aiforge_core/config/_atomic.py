"""The one atomic file-publish primitive for the whole codebase.

Every module that replaces a file in place — config registries, memory nodes,
work notes, perf samples — routes through here instead of hand-rolling
``open(path + ".tmp") ... os.replace``. That hand-rolled shape has a real bug:
the staging name is *shared*, so two writers of the same target (two processes,
or two threads without a common lock) write into the same file and the rename
publishes a blend of both bodies. A unique staging name per writer removes it.

Lives in ``config`` because that package imports nothing else from
``aiforge_core`` — it is the leaf of the import graph, so ``memory``,
``runtime`` and the rest can depend on it without any risk of a cycle.

What a write here guarantees:

* **One whole body.** Each writer stages into its own ``mkstemp`` file in the
  destination directory, so concurrent writers never share a buffer. The final
  ``os.replace`` is atomic within a filesystem, and staging in the *destination
  directory* is what keeps the rename on one filesystem. A reader sees the old
  body or the new one, never a mixture; the last rename wins.
* **Content durability.** The bytes are ``flush``ed and ``fsync``ed before the
  rename, so a crash cannot publish a truncated or zero-length file.
* **No litter.** The staging file is unlinked on any failure, and the original
  error is re-raised unchanged.

What it does NOT guarantee: the *rename* surviving a crash. The parent
directory is never fsynced, so a power loss immediately after ``os.replace``
may leave the previous content in place. That is the deliberate trade — a lost
write is re-done on the next cycle, a torn one would propagate — but callers
must not read "atomic" as "durable".
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

__all__ = ["write_bytes", "write_text"]


def _default_mode() -> int:
    """OWNER-ONLY (0600) for everything this module publishes.

    These are the files under AIFORGE_CONFIG_DIR, and `agent_config` persists an
    `api_key` (see agent_config/_persist.py). The old default was
    ``0o666 & ~umask`` — 0644 under the usual umask — so on any shared machine
    every local user could read the operator's API keys. `mkstemp` already
    creates at 0600; this used to widen it back out to match what a plain
    `open()` would have done, which is the wrong benchmark for a credential
    store. ~/.ssh and ~/.aws/credentials are the right comparison.

    AIFORGE_CONFIG_MODE overrides it (octal, e.g. "0640") for a deployment that
    genuinely needs group reads — but it has to be asked for.
    """
    raw = (os.environ.get("AIFORGE_CONFIG_MODE") or "").strip()
    if raw:
        try:
            return int(raw, 8)
        except ValueError:
            pass
    return 0o600


_MODE = _default_mode()


def write_bytes(target: str | Path, body: bytes, *, mode: int | None = None) -> None:
    """Publish ``body`` at ``target`` as a single visible step.

    Missing parent directories are created. See the module docstring for what
    is and is not guaranteed.

    ``mode`` overrides the permissions of the published file. Left as None it
    honours the umask, matching what a plain ``open(path, "w")`` would have
    produced — which is what every call site this replaced used. Pass it
    explicitly for content that should stay private regardless of umask; the
    memory tree does exactly that, because ``mkstemp``'s own 0600 would
    otherwise be widened here and quietly publish a peer's notes to every
    local account.
    """
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # ``os.fchmod`` is Unix-only; the name is ours and random
        os.chmod(tmp, _MODE if mode is None else mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_text(target: str | Path, text: str, *, encoding: str = "utf-8",
               mode: int | None = None) -> None:
    """``write_bytes`` for callers that hold a str. Same guarantees."""
    write_bytes(target, text.encode(encoding), mode=mode)
