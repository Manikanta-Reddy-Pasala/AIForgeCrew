"""Reading keys WHILE a run streams.

prompt_toolkit owns the keyboard only when it is asking for a line. During a
run the CLI is busy reading an event stream, so Esc (stop) and typing (steer)
have to come off raw stdin in the background. Two implementations — termios on
POSIX, msvcrt on Windows — and a no-op when stdin is not a tty, which is what
makes `aiforge "…" < /dev/null` and CI behave.
"""

from __future__ import annotations

import os
import queue
import sys
import threading

ESC = "\x1b"
ENTER = "\r"
BACKSPACE = "\x7f"
CTRL_C = "\x03"
CTRL_D = "\x04"


class KeyWatcher:
    """Context manager yielding single keypresses through :meth:`get`."""

    def __init__(self, stream=None):
        self._in = stream or sys.stdin
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restore = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        try:
            return bool(self._in.isatty())
        except Exception:  # noqa: BLE001 — a stub stream
            return False

    def __enter__(self) -> KeyWatcher:
        if not self.active:
            return self
        if os.name == "nt":
            self._thread = threading.Thread(target=self._loop_windows, daemon=True)
        else:
            self._raw()
            self._thread = threading.Thread(target=self._loop_posix, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._restore is not None:
            import termios
            termios.tcsetattr(self._in.fileno(), termios.TCSADRAIN, self._restore)
            self._restore = None

    def get(self) -> str | None:
        """The next keypress, or None if nothing is waiting."""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    # ── platforms ──────────────────────────────────────────────────────────

    def _raw(self) -> None:
        import termios
        import tty
        fd = self._in.fileno()
        self._restore = termios.tcgetattr(fd)
        tty.setcbreak(fd)          # cbreak, not raw: Ctrl+C still signals

    def _loop_posix(self) -> None:
        import select
        fd = self._in.fileno()
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                ch = os.read(fd, 1).decode("utf-8", "ignore")
            except OSError:
                return
            if ch:
                self._q.put(ch)

    def _loop_windows(self) -> None:
        import msvcrt
        import time
        while not self._stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                self._q.put("\r" if ch == "\n" else ch)
            else:
                time.sleep(0.05)
