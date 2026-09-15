"""One palette, resolved once.

Only the 8 ANSI colours plus bold/dim, and never a background: the user's
terminal theme decides what "green" looks like, so the output stays readable on
a light terminal as well as a dark one. Truecolor buys nothing for eight roles,
so the only real decision is colour or no colour.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# Roles, not colours. Call sites name what a line MEANS; this table decides how
# it looks, so a theme change is one edit here rather than a grep for "32m".
_SGR = {
    "user": "96",        # bright cyan — your own turn marker
    "thought": "2",      # dim
    "running": "33",     # yellow — a tool in flight
    "ok": "32",          # green
    "fail": "31",        # red
    "error": "1;31",     # bold red
    "approval": "35",    # magenta — the gate must not blend in
    "code": "36",        # cyan
    "link": "4;34",      # underlined blue
    "head": "1",         # bold
    "dim": "2",
    "warn": "33",
    "plus": "32",        # diff +
    "minus": "31",       # diff -
    "hunk": "2",         # diff @@
}


@dataclass(frozen=True)
class Palette:
    """Whether to emit SGR at all. `enabled=False` makes every call a no-op."""

    enabled: bool

    def __call__(self, text: str, role: str) -> str:
        if not self.enabled or not text:
            return text
        code = _SGR.get(role)
        return f"\033[{code}m{text}\033[0m" if code else text

    def ctx(self, pct: float) -> str:
        """Context-usage role: green, amber past 60%, red past 85%."""
        return "ok" if pct < 60 else ("warn" if pct < 85 else "fail")


def _looks_windows(env: dict[str, str]) -> bool:
    return os.name == "nt" or env.get("OS", "").startswith("Windows")


def enable_windows_ansi() -> None:
    """Turn on VT processing so the Windows console honours SGR escapes.

    Windows 10+ supports them but not by default for a console attached to an
    exe. Without this the binary prints the escape bytes literally.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32                       # type: ignore[attr-defined]
        for handle in (-11, -12):                               # stdout, stderr
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(kernel32.GetStdHandle(handle),
                                       ctypes.byref(mode)):
                kernel32.SetConsoleMode(kernel32.GetStdHandle(handle),
                                        mode.value | 0x0004)
    except Exception:  # noqa: BLE001 — an old console simply stays monochrome
        pass


def detect(stream=None, env: dict[str, str] | None = None) -> Palette:
    """Colour unless the environment says otherwise.

    Honours NO_COLOR (any value, per no-color.org), TERM=dumb, and a
    non-tty destination — a redirect or a pipe gets clean text, so
    `aiforge "…" > out.txt` is readable and diffable.
    """
    env = os.environ if env is None else env
    stream = sys.stdout if stream is None else stream
    if env.get("NO_COLOR") is not None:
        return Palette(False)
    if env.get("AIFORGE_CLI_COLOR") == "always":
        return Palette(True)
    if env.get("TERM", "") == "dumb":
        return Palette(False)
    # TERM is normally UNSET on Windows, where the console speaks ANSI once VT
    # processing is on (see enable_windows_ansi). Treating an empty TERM as
    # "no colour" made the entire Windows binary monochrome; only POSIX reads
    # an empty TERM as a dumb terminal.
    if not env.get("TERM") and not _looks_windows(env):
        return Palette(False)
    try:
        tty = bool(stream.isatty())
    except Exception:  # noqa: BLE001 — a stub stream without isatty()
        tty = False
    return Palette(tty)
