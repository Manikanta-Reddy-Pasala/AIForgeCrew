"""Shared unified-diff preview for the pre-apply review gate (Gap D).

Both the ADK Doer's tool gate (:mod:`tool_gate`) and the simple chat loop's
inline gate (:mod:`chat_agent`) need to show the human a REAL diff of a
file-mutating tool call against the file CURRENTLY on disk before the edit
lands. This factors that out so the two gates produce identical previews.

``unified_preview(path, new_text, cwd)`` resolves ``path`` against
``AIFORGE_REPO_ROOT`` (the Doer's repo root) or the supplied ``cwd``, reads
the existing file (empty string when it's a new file), and returns a unified
diff capped to ~3000 chars. Never raises — a preview failure degrades to a
best-effort body, it must not break the gate.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

_MAX_PREVIEW_CHARS = 3000


def _resolve(path: str, cwd: str) -> Path:
    """Resolve ``path`` against AIFORGE_REPO_ROOT (preferred) or ``cwd``."""
    if os.path.isabs(path):
        return Path(path)
    base = os.environ.get("AIFORGE_REPO_ROOT") or cwd or os.getcwd()
    return Path(base).expanduser() / path


def unified_preview(path: str, new_text: str, cwd: str = "",
                    max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    """Unified diff of ``new_text`` against the current file at ``path``.

    Reads the existing file (empty string if it doesn't exist → a new-file
    diff with all ``+`` lines). Capped to ``max_chars``. Never raises.
    """
    path = str(path or "?")
    new = new_text if isinstance(new_text, str) else str(new_text or "")
    try:
        old = _resolve(path, cwd).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — missing file / read error → treat as new
        old = ""
    # Bound difflib's O(n·m) cost on huge rewrites — this is a human glance.
    old_c, new_c = old[:40_000], new[:40_000]
    diff = "\n".join(difflib.unified_diff(
        old_c.splitlines(), new_c.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
    if diff.strip():
        return diff[:max_chars]
    # No textual change (or both empty) — still show something useful.
    if not old:
        return f"(new file {path}, {len(new)} bytes)\n{new[:max_chars]}"
    return "(no change)"


__all__ = ["unified_preview"]
