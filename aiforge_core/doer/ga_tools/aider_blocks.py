"""Aider SEARCH/REPLACE block parser — pure text → list[edit].

Aider's diff format: model emits one or more blocks like

    path/to/file.java
    <<<<<<< SEARCH
    old code
    =======
    new code
    >>>>>>> REPLACE

We recognise this format too so the doer can emit it directly
when natural (Aider/Sonnet style) instead of GA's tool_use JSON.
The blocks parse into the same dict shape ``bulk_edit`` expects:
``{path, old_content, new_content}``.

Pure function — no GA dependency. Use from ``do_bulk_edit`` or
the user_input fallback parser.
"""
from __future__ import annotations

import re
from typing import Iterator

_HEAD = "<" * 7 + " SEARCH"   # <<<<<<< SEARCH
_DIV = "=" * 7                # =======
_TAIL = ">" * 7 + " REPLACE"  # >>>>>>> REPLACE


def parse(text: str) -> list[dict]:
    """Return a list of ``{path, old_content, new_content}`` dicts.

    Tolerant: leading/trailing fences (```...```), Windows line
    endings, optional language tag after path. Empty list when no
    block found.
    """
    edits: list[dict] = []
    text = text.replace("\r\n", "\n")
    # Walk the text line-by-line; cheaper than a giant regex and
    # easier to follow when blocks are interleaved with prose.
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if _HEAD not in lines[i]:
            i += 1
            continue
        # Path = the closest non-empty, non-fence line before HEAD.
        path = ""
        j = i - 1
        while j >= 0:
            stripped = lines[j].strip().strip("`")
            if stripped and not stripped.startswith("//"):
                path = stripped.split()[0]
                break
            j -= 1
        # Collect old_content until DIV.
        i += 1
        old_lines: list[str] = []
        while i < n and _DIV not in lines[i]:
            old_lines.append(lines[i])
            i += 1
        if i >= n:
            break  # malformed
        # Skip the divider line.
        i += 1
        new_lines: list[str] = []
        while i < n and _TAIL not in lines[i]:
            new_lines.append(lines[i])
            i += 1
        if path:
            edits.append({
                "path": path,
                "old_content": "\n".join(old_lines),
                "new_content": "\n".join(new_lines),
            })
        i += 1  # past TAIL
    return edits


def iter_blocks(text: str) -> Iterator[dict]:
    yield from parse(text)
