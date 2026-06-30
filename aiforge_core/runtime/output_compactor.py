"""Deterministic compaction of noisy tool output.

``bash`` / ``run_shell`` already cap each stream at 8 KB. The remaining
problem for a slow 120B model is signal-to-noise: a 200-line pytest dump
where the only thing that matters is "NullPointerException at Foo.java:42".

:func:`digest` pulls the salient lines — errors, failures, tracebacks,
assertion mismatches, non-zero exit — into a short block so the model (and the
executor-focus history replay) leads with signal, not scrollback. Pure regex,
no LLM, no added latency. An optional small-model summary can be layered on by
callers, but the default is deterministic.

Env:
  AIFORGE_COMPACT_OUTPUT=0     disable (callers return raw output only)
  AIFORGE_COMPACT_MAX_LINES=24 salient lines to keep
  AIFORGE_COMPACT_MAX_CHARS=1400 hard cap on the digest block
"""
from __future__ import annotations

import os
import re

# Lines worth surfacing. Case-insensitive substring/anchor patterns covering
# the common failure shapes across python / java / node / go / shells.
_SIGNAL_RE = re.compile(
    r"(error|exception|traceback|fail(ed|ure)?|fatal|panic|assert"
    r"|no such|not found|cannot|undefined|unresolved|syntaxerror"
    r"|\bE\s|^\s*at\s|::error|exit code|non-zero|segmentation)",
    re.IGNORECASE,
)


def _enabled() -> bool:
    return os.environ.get("AIFORGE_COMPACT_OUTPUT", "1") not in ("0", "false", "no")


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def key_lines(text: str, max_lines: int | None = None) -> list[str]:
    """Salient lines from ``text`` (order-preserving, de-duped, capped)."""
    if not text:
        return []
    cap = max_lines if max_lines is not None else _int_env("AIFORGE_COMPACT_MAX_LINES", 24)
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or not _SIGNAL_RE.search(line):
            continue
        key = line.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= cap:
            break
    return out


def digest(stdout: str = "", stderr: str = "", returncode=None) -> str:
    """Compact failure digest, or ``""`` when nothing salient is found.

    Combines key lines from stderr (first — usually the real error) then
    stdout, prefixed with the exit code. Hard-capped so it can never re-bloat
    the context it's meant to shrink."""
    if not _enabled():
        return ""
    lines = key_lines(stderr) + [ln for ln in key_lines(stdout)
                                 if ln not in set(key_lines(stderr))]
    if not lines and (returncode in (None, 0)):
        return ""
    head = "" if returncode is None else f"exit={returncode} · "
    body = "\n".join(lines)
    out = (head + ("key lines:\n" + body if body else "command failed")).strip()
    return out[:_int_env("AIFORGE_COMPACT_MAX_CHARS", 1400)]


__all__ = ["key_lines", "digest"]
