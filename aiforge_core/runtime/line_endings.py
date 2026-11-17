"""Edits keep a file's line endings.

Python's text read turns ``\\r\\n`` into ``\\n`` and its text write writes
``\\n``, so every edit to a Windows-style (CRLF) file rewrote all of its
lines: a real 30-line C++ fix became a 6,078-line diff. Edit tools read as
before (``\\n`` inside, so a model's ``old_str`` matches) and write through
:func:`write`, which puts back the file's own style.
"""
from __future__ import annotations

import os

CRLF, LF = "\r\n", "\n"


def style_of(path) -> "str | None":
    """``"\\r\\n"`` when most of the file's line breaks are CRLF, ``"\\n"`` when
    not, None when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return CRLF if crlf > lf else LF


def write(path, text: str, style: "str | None" = None) -> None:
    """Write ``text`` with ``style`` line endings (default: the file's own when
    it exists, else the text's own)."""
    if style is None:
        style = style_of(path) if os.path.isfile(path) else (CRLF if CRLF in text else LF)
    body = text.replace(CRLF, LF)
    if style == CRLF:
        body = body.replace(LF, CRLF)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
