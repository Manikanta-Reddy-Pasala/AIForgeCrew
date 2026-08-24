"""One helper for launching bounded background work.

~30 sites hand-rolled ``threading.Thread(target=..., daemon=True).start()`` (and
8 ``subprocess.Popen``), each reinventing a different subset of: a diagnosable
``name``, an error sink, and — critically — the "CPU-bound work must be a PROCESS
so it doesn't starve uvicorn's event loop" rule (which was tribal lore, not
enforced). This centralizes all three.

    spawn(fn, name="chat-learn")                 # daemon thread, errors logged
    spawn(fn, name="reindex", kind="process",    # separate process (GIL-free)
          argv=[sys.executable, "-m", "mod"])

``kind="thread"`` (default) runs ``fn`` on a daemon thread with a top-level
``except`` that logs instead of dying silently. ``kind="process"`` launches
``argv`` detached (own GIL) — use it for CPU-bound work (indexing, embedding).
Never raises; returns the ``Thread`` / ``Popen`` handle (or ``None`` on a
launch failure).
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable

log = logging.getLogger("aiforge.background")


def _spawn_process(argv: "list[str] | None", name: str):
    """Detached child process. Best-effort — never raises."""
    if not argv:
        log.warning("background.spawn(kind=process) needs argv (name=%s)", name)
        return None
    try:
        return subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("background process spawn failed (%s): %s", name, exc)
        return None


def _guarded(fn, name: str, on_error) -> "Callable[[], None]":
    """Wrap the callable so a failure is REPORTED, never swallowed.

    Split out because it is the one piece with real behaviour to get right:
    an `on_error` that itself raises must not lose the original exception.
    """
    def _run() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a background task must never
            # die silently; surface it instead of the bare `except: pass` idiom.
            if on_error is not None:
                try:
                    on_error(exc)
                    return
                except Exception:  # noqa: BLE001
                    pass
            log.warning("background task %r failed: %s", name, exc)
    return _run


def _spawn_thread(fn, name: str, on_error):
    """Daemon thread running ``fn``. Best-effort — never raises."""
    if fn is None:
        log.warning("background.spawn(kind=thread) needs fn (name=%s)", name)
        return None
    try:
        t = threading.Thread(target=_guarded(fn, name, on_error),
                             name=name, daemon=True)
        t.start()
        return t
    except Exception as exc:  # noqa: BLE001
        log.warning("background thread spawn failed (%s): %s", name, exc)
        return None


def spawn(fn: "Callable[[], object] | None" = None, *, name: str,
          kind: str = "thread",
          argv: "list[str] | None" = None,
          on_error: "Callable[[BaseException], None] | None" = None):
    """Launch background work. See module docstring. Best-effort — never raises.

    Dispatch only: a process and a thread share nothing but this entry point,
    so they are separate functions rather than two halves of one `if`.
    """
    if kind == "process":
        return _spawn_process(argv, name)
    return _spawn_thread(fn, name, on_error)


__all__ = ["spawn"]
