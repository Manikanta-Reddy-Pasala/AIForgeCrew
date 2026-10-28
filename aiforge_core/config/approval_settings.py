"""Per-CHAT-MODE approval switch — whether a chat run of a given mode pauses
for human Approve/Reject on ``ask``-policy or review-gated tool calls.

Three modes: Chat (simple), Plan, Pipeline (team). Each is a boolean
"require approval". When OFF, the tool gate does NOT pause that mode's runs for
approval (``ask`` and review-edits gates auto-allow). When ON (the default),
behaviour is unchanged.

A hard ``deny`` policy still blocks either way — that is a safety floor, not a
chat-mode approval, and the toggle cannot lift it.

DESTRUCTIVE DELETES: in an INTERACTIVE chat run, approvals-off IS the
confirmation — see ``_command_gate_flags`` in runtime/chat_agent/_turn/_approval.py. It
did not used to be, and the result was a toggle that felt broken: the delete
guard matches the whole command string, so routine remote maintenance
(``ssh host 'docker rm -f c'``, ``kubectl delete pod``, ``git clean -fdx``)
kept prompting after the operator had explicitly turned approvals off. The
relaxation is audited and scoped to the mode whose toggle is off.

Autonomous ticket runs (no chat session) are unaffected in BOTH directions —
they never had a human approver, so ``required(None)`` is True by construction
and no toggle can relax a delete for an unattended run.
Stored at ``$AIFORGE_CONFIG_DIR/approval_settings.json``.

PER CHAT: the per-mode values above are the DEFAULTS. A chat that flips its
own toggle keeps that choice for itself only (``set_session``), stored in
``$AIFORGE_CONFIG_DIR/chat-approvals.json`` as ``{session_id: {mode: bool}}``.
Turning approvals off in one chat used to switch them off in every chat of
that mode. A deleted chat's choice goes with it (``forget_session``).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from aiforge_core.config import _atomic
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


def required(mode: str, session_id: int | None = None) -> bool:
    """True if a run of ``mode`` (Chat/Plan/Pipeline aliases accepted) should
    pause for approval. A chat's own choice wins over the mode default.
    Unknown/blank mode → default ON (fail safe)."""
    m = _canon(mode)
    if m not in _MODES:
        return _DEFAULT
    if session_id is not None:
        chats = _session_load()
        if chats is None:      # unreadable: a chat's "on" may be in there
            return True
        own = chats.get(str(session_id))
        if isinstance(own, dict) and m in own:
            return bool(own[m])
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


def _session_path() -> Path:
    return Path(os.path.expanduser(str(config_dir()))) / "chat-approvals.json"


def _session_load() -> dict | None:
    """Every chat's choices; {} when there is no file, None when it cannot be
    read (callers then fail safe to approvals ON)."""
    p = _session_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _session_save(data: dict) -> None:
    p = _session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_text(str(p), json.dumps(data, indent=1))


def session_modes(session_id: int) -> dict[str, bool]:
    """Full mode→required map for one chat: its own choices over the defaults."""
    return {m: required(m, session_id) for m in _MODES}


def set_session(session_id: int, mode: str, on: bool) -> dict[str, bool]:
    """Enable/disable approvals for ``mode`` in ONE chat. Returns its map."""
    m = _canon(mode)
    if m not in _MODES:
        raise ValueError(f"unknown mode: {mode!r} (use chat/plan/pipeline)")
    with _LOCK:
        data = _session_load()
        if data is None:
            # Starting over would silently drop another chat's "on".
            raise OSError(f"{_session_path()} is unreadable — fix or delete it")
        own = data.get(str(session_id))
        own = dict(own) if isinstance(own, dict) else {}
        own[m] = bool(on)
        data[str(session_id)] = own
        _session_save(data)
    return session_modes(session_id)


def copy_session(from_id: int, to_id: int) -> None:
    """Give ``to_id`` the same own choices as ``from_id`` (a side task)."""
    with _LOCK:
        data = _session_load()
        own = (data or {}).get(str(from_id))
        if data is None or not isinstance(own, dict):
            return
        data[str(to_id)] = dict(own)
        _session_save(data)


def forget_session(session_id: int | None = None) -> None:
    """Drop one chat's choices, or every chat's when ``session_id`` is None
    (ids restart at 1 after delete-all, so leftovers would leak into new chats)."""
    with _LOCK:
        if session_id is None:
            if _session_path().exists():
                _session_save({})
            return
        data = _session_load()
        if data is not None and data.pop(str(session_id), None) is not None:
            _session_save(data)


__all__ = ["all_modes", "copy_session", "forget_session", "required", "session_modes",
           "set_mode", "set_session"]
