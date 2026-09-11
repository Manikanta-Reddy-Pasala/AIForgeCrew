"""Folders a chat session was ALLOWED to write outside its workspace.

The workspace jail refused a write outside the chat's folder and the agent was
left to talk the user into it — including telling them to set an AIForge
environment variable, which is no answer for a user. Now the refusal is an
approval: the user clicks Allow once, the folder (its git repo when it is in
one) is recorded here for the session, and every later turn of that chat may
write there. A grant is per session, never global; an unattended run has no
one to ask and stays refused.

Stored as one small JSON file in the config dir so a grant survives an API
restart. Soft-fail: an unreadable file means no grants (the jail asks again).
"""
from __future__ import annotations

import json
import os
import threading

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()


def _path() -> str:
    return os.path.join(str(config_dir()), "chat-write-grants.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt → no grants
        return {}


def granted(session_id) -> list[str]:
    """The folders ``session_id`` may write to beyond its workspace."""
    if session_id is None:
        return []
    with _LOCK:
        roots = _load().get(str(session_id)) or []
    return [r for r in roots if isinstance(r, str) and r]


def grant(session_id, roots) -> None:
    """Record ``roots`` as writable for ``session_id``."""
    if session_id is None:
        return
    with _LOCK:
        data = _load()
        have = list(data.get(str(session_id)) or [])
        for r in roots or ():
            if r and r not in have:
                have.append(r)
        data[str(session_id)] = have
        try:
            os.makedirs(os.path.dirname(_path()), exist_ok=True)
            _atomic.write_text(_path(), json.dumps(data, indent=1))
        except Exception:  # noqa: BLE001 — the in-turn grant still applies
            pass


def forget(session_id) -> None:
    """Drop a deleted session's grants."""
    with _LOCK:
        data = _load()
        if data.pop(str(session_id), None) is not None:
            try:
                _atomic.write_text(_path(), json.dumps(data, indent=1))
            except Exception:  # noqa: BLE001
                pass


def forget_all() -> None:
    """Drop every grant (all chats deleted; ids restart at 1)."""
    with _LOCK:
        try:
            if os.path.exists(_path()):
                os.remove(_path())
        except Exception:  # noqa: BLE001
            pass


def grant_root(target: str) -> str:
    """The folder one approval covers for ``target``: its git repository when
    it is inside one (approving a write to one file of a project means the
    project), else the nearest folder that exists. Never ``/`` or the home
    directory itself — too broad to grant by one click; those fall back to the
    nearest existing folder."""
    home = os.path.realpath(os.path.expanduser("~"))
    d = os.path.realpath(target)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    nearest = d
    probe = d
    while probe and probe not in (os.sep, home):
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if nearest in (os.sep, home):
        # /home/me/newproj/x.py → grant the folder being created, not ~.
        parent = os.path.dirname(os.path.realpath(target))
        return parent if parent not in (os.sep, home) else os.path.realpath(target)
    return nearest


__all__ = ["granted", "grant", "forget", "forget_all", "grant_root"]
