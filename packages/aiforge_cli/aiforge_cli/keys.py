"""Reading keys WHILE a run streams.

prompt_toolkit owns the keyboard only when it is asking for a line. During a
run the CLI is busy reading an event stream, so Esc (stop) and typing (steer)
have to come off raw stdin in the background. Two implementations — termios on
POSIX, msvcrt on Windows — and a no-op when stdin is not a tty, which is what
makes `aiforge "…" < /dev/null` and CI behave.

Escape needs care: an arrow key sends ``\\x1b[A``, so a bare byte comparison
turned every Up press into "stop the run". A lone ``\\x1b`` is only Esc when
nothing follows it, so the reader buffers it briefly and swallows the rest of a
CSI/SS3 sequence.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time

ESC = "\x1b"
ENTER = "\r"
BACKSPACE = "\x7f"
CTRL_C = "\x03"
CTRL_D = "\x04"

# How long to wait for the second byte of an escape sequence. Terminals send
# the whole sequence in one burst, so this is about scheduling, not typing.
ESC_GRACE = 0.06


class KeyWatcher:
    """Context manager yielding single keypresses through :meth:`get`.

    ``source`` exists for tests: a queue-fed watcher behaves exactly like a
    terminal one, so Esc / steer / interrupt semantics are testable headlessly.
    """

    def __init__(self, stream=None, *, source: queue.Queue[str] | None = None):
        self._in = stream or sys.stdin
        self._q: queue.Queue[str] = source or queue.Queue()
        self._injected = source is not None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._restore = None
        self._paused = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        if self._injected:
            return True
        try:
            return bool(self._in.isatty())
        except Exception:  # noqa: BLE001 — a stub stream
            return False

    def __enter__(self) -> KeyWatcher:
        if self._injected or not self.active:
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
        thread, self._thread = self._thread, None
        if thread is not None:
            # Join before restoring the mode: a reader still inside os.read()
            # would otherwise swallow the first key of the next prompt.
            thread.join(timeout=ESC_GRACE * 10)
        self._restore_mode()

    def pause(self) -> None:
        """Hand the terminal back — for a prompt that needs a whole line.

        Two readers on one fd race for every byte, so an approval prompt cannot
        use input() while the watcher thread is alive.
        """
        if self._injected or self._paused:
            return
        self._paused = True
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=ESC_GRACE * 10)
        self._restore_mode()
        self.drain()

    def resume(self) -> None:
        if self._injected or not self._paused:
            return
        self._paused = False
        self._stop = threading.Event()
        self.__enter__()

    def drain(self) -> int:
        """Throw away anything typed but unread; returns how much.

        Called before a prompt takes the terminal back, so keystrokes meant for
        the run do not arrive as an answer to a question.
        """
        dropped = 0
        while self.get() is not None:
            dropped += 1
        return dropped

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

    def _restore_mode(self) -> None:
        if self._restore is None:
            return
        try:
            import termios
            termios.tcsetattr(self._in.fileno(), termios.TCSADRAIN, self._restore)
        except Exception:  # noqa: BLE001 — the tty may be gone already
            pass
        self._restore = None

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
            if not ch:
                continue
            if ch == ESC and self._swallow_sequence(fd, select):
                continue
            self._q.put(ch)

    def _swallow_sequence(self, fd, select) -> bool:
        """True when this ESC began an arrow/function key, not a stop.

        The rest of the sequence is read and dropped, so pressing Up during a
        run neither stops it nor types `[A` into the steer buffer.
        """
        ready, _, _ = select.select([fd], [], [], ESC_GRACE)
        if not ready:
            return False                      # a real, lonely Esc
        try:
            nxt = os.read(fd, 1).decode("utf-8", "ignore")
        except OSError:
            return True
        if nxt not in ("[", "O"):
            self._q.put(nxt)                  # Alt+<key>: keep the key
            return True
        deadline = time.monotonic() + ESC_GRACE
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], ESC_GRACE)
            if not ready:
                break
            try:
                tail = os.read(fd, 1).decode("utf-8", "ignore")
            except OSError:
                break
            if tail.isalpha() or tail == "~":
                break                          # final byte of the sequence
        return True

    def _loop_windows(self) -> None:
        import msvcrt
        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()                # the arrow/function key itself
                continue
            self._q.put("\r" if ch == "\n" else ch)
