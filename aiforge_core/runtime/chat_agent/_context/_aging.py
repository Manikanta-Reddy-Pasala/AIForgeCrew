"""Old, large tool output leaves the prompt before it forces a condense.

A file read stays in the history at full size for the rest of the run, and in
a long task most of the prompt is reads the model finished with many steps ago.
Once an observation is more than a few messages old, its full text is saved
(``runtime.context_offload``) and the history keeps a head, a tail and the id:
``memory_lookup {"id": ...}`` or a fresh read brings it back. Only reads and
searches age; command output, errors and edit results stay as they are.
``AIFORGE_CHAT_AGE_OBS=0`` turns it off.
"""
from __future__ import annotations

import os

from .._shell import _ACTION_RE

#: Output of these is re-readable from disk or the web, so ageing loses nothing.
_AGEABLE = frozenset({"file_read", "read_files", "read_lines", "grep", "find",
                      "list_dir", "web_fetch", "web_crawl", "git_diff",
                      "git_log", "git_blame", "codegraph_query",
                      "codegraph_explore"})
_MARK = "[aged:"
_HEAD, _TAIL = 700, 400


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_AGE_OBS", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _int_env(key: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(key, default)))
    except ValueError:
        return default


def age_observations(convo: list) -> int:
    """Shrink old, large read/search observations in place. Returns how many."""
    if not _on() or not convo:
        return 0
    keep = _int_env("AIFORGE_CHAT_AGE_KEEP", 10)       # newest messages untouched
    floor = _int_env("AIFORGE_CHAT_AGE_MIN_CHARS", 3000)
    from aiforge_core.runtime import context_offload
    aged = 0
    for i in range(1, max(1, len(convo) - keep)):
        m = convo[i]
        text = m.get("content") if isinstance(m, dict) else None
        if (m.get("role") != "user" or not isinstance(text, str)
                or not text.startswith("OBSERVATION:") or _MARK in text
                or len(text) < floor):
            continue
        prev = convo[i - 1]
        mt = _ACTION_RE.search(str(prev.get("content") or "")) \
            if prev.get("role") == "assistant" else None
        if not mt or mt.group(1).lower() not in _AGEABLE:
            continue
        oid = context_offload.save(text)
        if not oid:
            continue
        body = text[len("OBSERVATION:"):].strip()
        convo[i] = {**m, "content": (
            f"OBSERVATION: {_MARK} {mt.group(1)} output, {len(body)} chars — "
            f'full text saved: memory_lookup {{"id": "{oid}"}}, or read it '
            f"again]\n{body[:_HEAD]}\n…\n{body[-_TAIL:]}")}
        aged += 1
    return aged
