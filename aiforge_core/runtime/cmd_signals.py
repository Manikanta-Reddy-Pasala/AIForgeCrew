"""What a running command's NEW output says: a failure, or a prompt waiting
for input — the things worth interrupting a wait for, so the agent can kill
and fix at once instead of sitting out the rest of a build that already broke.

Deliberately a small, local pattern set (no dependency on the repair loops'
failure fingerprinting): it only has to answer "stop waiting and look?".
"""
from __future__ import annotations

import os
import re

#: Bytes of a stream scanned / shown per look; more is summarised as skipped.
SCAN_BYTES = 64 * 1024

_FAILURES = (
    ("traceback", re.compile(r"^Traceback \(most recent call last\)", re.M)),
    ("npm error", re.compile(r"^npm (?:ERR!|error)", re.M)),
    ("build failure", re.compile(r"BUILD FAILURE|BUILD FAILED|FAILURE: Build failed")),
    ("panic", re.compile(r"^(?:thread '.*' )?panic(?:ked)?[: ]", re.M)),
    ("port in use", re.compile(r"Address already in use|EADDRINUSE", re.I)),
    ("command not found", re.compile(r"command not found|: not found$", re.M)),
    ("permission denied", re.compile(r"Permission denied|EACCES")),
    ("FAILED", re.compile(r"(?<![\w-])FAILED\b")),
    ("ERROR", re.compile(r"(?<![\w-])ERROR\b")),
    ("error:", re.compile(r"(?:^|\s)(?:error|fatal)(?:\[\w+\])?:\s", re.M)),
)
#: The output ENDS (no newline) on something that waits for a person.
_PROMPT = re.compile(
    r"(?:\[[yY]/[nN]\]|\[[nN]/[yY]\]|\([yY]/[nN]\)|\(yes/no\)"
    r"|[Pp]ass(?:word|phrase)[^\n]*:|\?)\s*$")


def failure_in(text: str) -> str | None:
    """``"<kind>: <line>"`` for the first failure line in ``text``, else None."""
    if not text:
        return None
    for kind, rx in _FAILURES:
        m = rx.search(text)
        if m:
            start = text.rfind("\n", 0, m.end()) + 1
            end = text.find("\n", m.end())
            line = text[start:end if end >= 0 else None].strip()
            return f"{kind}: {line[:160]}"
    return None


def waiting_for_input(tail: str) -> str | None:
    """The last line, when the output stops on a prompt with no newline."""
    if not tail or tail.endswith("\n"):
        return None
    last = tail.rsplit("\n", 1)[-1]
    if _PROMPT.search(last[-200:]):
        return f"waiting for input: {last.strip()[:120]}"
    return None


def signal_in(new_text: str, tail: str) -> str | None:
    return failure_in(new_text) or waiting_for_input(tail)


# ── reading a growing file without disturbing anyone else's position ─────

def file_size(fh) -> int:
    try:
        if isinstance(fh, str):
            return os.path.getsize(fh)
        fh.flush()
        return os.fstat(fh.fileno()).st_size
    except (OSError, ValueError):
        return 0


def read_range(fh, start: int, end: int) -> bytes:
    """Bytes ``[start, end)`` of an open file object or a path. ``os.pread``
    for a file object, so a concurrent reader's seek position is untouched."""
    n = max(0, end - start)
    if not n:
        return b""
    try:
        if isinstance(fh, str):
            with open(fh, "rb") as f:
                f.seek(start)
                return f.read(n)
        return os.pread(fh.fileno(), n, start)
    except (OSError, ValueError):
        return b""


def clean(raw: bytes) -> str:
    """Decoded, with carriage-return redraws (progress bars) collapsed to what
    each line finally showed."""
    text = raw.decode("utf-8", "replace")
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in lines)


def bounded(text: str, limit: int) -> str:
    """Head + tail of ``text`` within ``limit`` chars (the end matters most)."""
    if len(text) <= limit:
        return text
    head = limit // 6
    return (text[:head] + f"\n… ({len(text) - limit} chars skipped) …\n"
            + text[-(limit - head):])


__all__ = ["SCAN_BYTES", "bounded", "clean", "failure_in", "file_size", "job_hint",
           "read_range", "signal_in", "waiting_for_input"]


def job_hint(key, alive: bool, why: str | None = None) -> str:
    """What the model should do next with a handed-back job."""
    if not alive:
        return "finished — the output above is final."
    if why and any(why.startswith(kind + ":") for kind, _rx in _FAILURES):
        # Live: the model was told only "command_wait to wait for more" and
        # waited 45 s on a build it had already seen fail.
        return (f"an error appeared while it is still running. If it means "
                f"the command failed, command_kill(id='{key}') now and "
                f"report or fix it — do not wait for it to finish. If the "
                f"error is harmless, command_wait(id='{key}').")
    return (f"still running. command_wait(id='{key}') to wait for more "
            f"(returns early on an error, a prompt or a stall), "
            f"command_output(id='{key}') to peek, command_kill(id="
            f"'{key}') to stop it and fix the command.")
