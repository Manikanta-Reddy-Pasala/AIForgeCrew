"""Per-CHAT-MODE approval switch — whether a chat run of a given mode pauses
for human Approve/Reject on ``ask``-policy or review-gated tool calls.

Three modes: Chat (simple), Plan, Pipeline (team). Each is a boolean
"require approval". When OFF, the tool gate does NOT pause that mode's runs for
approval (``ask`` and review-edits gates auto-allow); a hard ``deny`` policy
still blocks, and a destructive-delete confirm still fires — those are safety
floors, not chat-mode approvals. When ON (the default), behaviour is unchanged.

Autonomous ticket runs (no chat session) are unaffected — they never had a
human approver anyway. Stored at ``$AIFORGE_CONFIG_DIR/approval_settings.json``.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()
_MODES = ("simple", "plan", "team")
# UI labels → internal mode keys.
_ALIAS = {"chat": "simple", "pipeline": "team", "": "simple"}
# Default: approvals ON everywhere (no behaviour change until toggled off).
_DEFAULT = True


def _canon(mode: str) -> str:
    m = (mode or "").strip().lower()
    return _ALIAS.get(m, m)


def _path() -> Path:
    root = Path(os.path.expanduser(
        str(config_dir())))
    return root / "approval_settings.json"


def _load() -> dict[str, bool]:
    p = _path()
    if p.exists():
        try:
            raw = json.loads(p.read_text()) or {}
            return {k: bool(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def all_modes() -> dict[str, bool]:
    """Full mode→required map, defaults filled in."""
    d = _load()
    return {m: bool(d.get(m, _DEFAULT)) for m in _MODES}


def required(mode: str) -> bool:
    """True if a run of ``mode`` (Chat/Plan/Pipeline aliases accepted) should
    pause for approval. Unknown/blank mode → default ON (fail safe)."""
    m = _canon(mode)
    if m not in _MODES:
        return _DEFAULT
    return bool(_load().get(m, _DEFAULT))


def set_mode(mode: str, on: bool) -> dict[str, bool]:
    """Enable/disable approvals for one mode. Returns the full map."""
    m = _canon(mode)
    if m not in _MODES:
        raise ValueError(f"unknown mode: {mode!r} (use chat/plan/pipeline)")
    with _LOCK:
        d = _load()
        d[m] = bool(on)
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=2))
    return all_modes()


__all__ = ["all_modes", "required", "set_mode"]
