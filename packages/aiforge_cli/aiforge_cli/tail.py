"""The one line that gets redrawn.

Everything else the CLI prints is permanent and scrolls like normal output —
that is what keeps copy/paste, `less` and `| tee` working. Only the bottom
status line is rewritten in place, so it has to be erased before anything is
committed above it and drawn again afterwards. This class is the only code that
moves the cursor.
"""

from __future__ import annotations

import itertools
import shutil
import sys
from collections.abc import Iterable

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Tail:
    def __init__(self, stream=None, *, enabled: bool | None = None, pal=None):
        self._out = stream or sys.stdout
        if enabled is None:
            try:
                enabled = bool(self._out.isatty())
            except Exception:  # noqa: BLE001 — a stub stream
                enabled = False
        self.enabled = enabled
        self._frames = itertools.cycle(SPINNER)
        self._shown = ""
        self._pal = pal

    # ── the bottom line ────────────────────────────────────────────────────

    def set(self, text: str | None) -> None:
        """Replace the status line. None clears it."""
        if not self.enabled:
            return
        if text is None:
            self.clear()
            return
        self._shown = text
        self._draw(text)

    def spin(self) -> None:
        """Advance the spinner without changing the words."""
        if self.enabled and self._shown:
            self._draw(self._shown)

    def clear(self) -> None:
        if not self.enabled:
            return
        self._out.write("\r\033[2K")
        self._out.flush()
        self._shown = ""

    # ── committing permanent output ────────────────────────────────────────

    def write(self, lines: Iterable[str]) -> None:
        """Print lines above the status line, then put it back."""
        lines = [line for line in lines]
        if not lines:
            return
        held = self._shown
        self.clear()
        for line in lines:
            self._out.write(line + "\n")
        self._out.flush()
        if held:
            self.set(held)

    def stream(self, text: str) -> None:
        """Append text with no newline — the answer as it is written."""
        if not text:
            return
        held = self._shown
        self.clear()
        self._out.write(text)
        self._out.flush()
        self._shown = held        # redrawn on the next set(), not mid-word

    def _draw(self, text: str) -> None:
        frame = next(self._frames)
        if self._pal is not None:
            frame = self._pal(frame, "code")
        width = shutil.get_terminal_size((100, 24)).columns - 2
        line = f"{frame} {text}"
        self._out.write("\r\033[2K" + line[:max(width, 20)])
        self._out.flush()
