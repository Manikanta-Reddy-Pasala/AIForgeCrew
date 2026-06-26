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

# Returned in place of file content when ``path`` resolves OUTSIDE the repo
# root — reading it would leak secrets (e.g. /etc/passwd, ~/.aiforge config)
# into the approval preview / transcript.
_SUPPRESSED = "[diff preview suppressed: path outside repo]"


def _root() -> str:
    """The repo root the preview is confined to (realpath-resolved)."""
    base = os.environ.get("AIFORGE_REPO_ROOT") or os.getcwd()
    return os.path.realpath(os.path.expanduser(base))


def _resolve(path: str, cwd: str) -> Path | None:
    """Resolve ``path`` to a real path and REFUSE anything outside the repo.

    Returns the resolved ``Path`` when it is contained within the repo root,
    or ``None`` when it escapes (absolute outside path, ``../`` traversal,
    symlink pointing out). Never raises.
    """
    try:
        root = os.path.realpath(
            os.path.expanduser(os.environ.get("AIFORGE_REPO_ROOT")
                               or cwd or os.getcwd()))
        if os.path.isabs(path):
            target = os.path.realpath(os.path.expanduser(path))
        else:
            target = os.path.realpath(os.path.join(root, path))
        # Contained iff target == root or target is under root/.
        if target == root or target.startswith(root + os.sep):
            return Path(target)
    except Exception:  # noqa: BLE001
        return None
    return None


def unified_preview(path: str, new_text: str, cwd: str = "",
                    max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    """Unified diff of ``new_text`` against the current file at ``path``.

    Reads the existing file (empty string if it doesn't exist → a new-file
    diff with all ``+`` lines). Capped to ``max_chars``. Never raises.

    SECURITY: only files CONTAINED within the repo root are read. A ``path``
    that resolves outside the repo (absolute outside path, ``../`` traversal,
    or an escaping symlink) is refused — the preview shows a redacted
    placeholder instead of the file's contents, so secrets never leak into
    the approval preview / transcript.
    """
    path = str(path or "?")
    new = new_text if isinstance(new_text, str) else str(new_text or "")
    resolved = _resolve(path, cwd)
    if resolved is None:
        return _SUPPRESSED
    try:
        old = resolved.read_text(encoding="utf-8", errors="replace")
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
