"""The one line that gets redrawn.

Everything else the CLI prints is permanent and scrolls like normal output —
that is what keeps copy/paste, `less` and `| tee` working. Only the bottom
status line is rewritten in place, so it has to be erased before anything is
committed above it and drawn again afterwards. This class is the only code that
moves the cursor.

The subtle part is streamed text. The answer arrives as fragments with no
newline, so the cursor sits in the middle of a line the user wants to keep. A
status redraw at that moment would erase it (`\\r` + erase-line takes the whole
line, not just the status). So a streamed fragment marks the line DIRTY: the
status line is suppressed until the stream ends, and anything committed
afterwards starts by closing that line with a newline.
"""

from __future__ import annotations

import itertools
import re
import shutil
import sys
from collections.abc import Iterable

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Carriage return + erase-the-whole-line: how the status line is replaced in
# place. Hex rather than octal escapes, which read as a file mode.
ERASE_LINE = "\r\x1b[2K"
RESET = "\x1b[0m"
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Width on screen — colour escapes cost no columns."""
    return len(_SGR.sub("", text))


def _truncate(text: str, width: int) -> str:
    """Cut to ``width`` VISIBLE characters, never mid-escape.

    Slicing the raw string counted the SGR bytes as columns (so a coloured tail
    truncated early) and could cut an escape in half, leaking colour into the
    rest of the terminal.
    """
    if _visible_len(text) <= width:
        return text
    out: list[str] = []
    shown = 0
    i = 0
    while i < len(text) and shown < width:
        match = _SGR.match(text, i)
        if match:
            out.append(match.group())
            i = match.end()
            continue
        out.append(text[i])
        shown += 1
        i += 1
    return "".join(out) + RESET


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
        self._dirty = False            # streamed text sits on the current line
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
        if self._dirty:
            return                     # would erase the answer being written
        self._draw(text)

    def spin(self) -> None:
        """Advance the spinner without changing the words."""
        if self.enabled and self._shown and not self._dirty:
            self._draw(self._shown)

    def clear(self) -> None:
        """Erase the status line, or close a streamed line, leaving neither."""
        if self._dirty:
            self._out.write("\n")
            self._dirty = False
            self._out.flush()
        elif self.enabled:
            self._out.write(ERASE_LINE)
            self._out.flush()
        self._shown = ""

    # ── committing permanent output ────────────────────────────────────────

    def write(self, lines: Iterable[str]) -> None:
        """Print lines above the status line, then put it back."""
        lines = list(lines)
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
        if self.enabled and not self._dirty:
            self._out.write(ERASE_LINE)       # take the status line's row
        self._out.write(text)
        self._out.flush()
        # Tracked even when disabled: a redirect has no status line to erase,
        # but the next permanent line still has to start on its own row rather
        # than being glued onto the streamed fragment.
        self._dirty = True

    def _draw(self, text: str) -> None:
        frame = next(self._frames)
        if self._pal is not None:
            frame = self._pal(frame, "code")
        width = max(shutil.get_terminal_size((100, 24)).columns - 2, 20)
        self._out.write(ERASE_LINE + _truncate(f"{frame} {text}", width))
        self._out.flush()
