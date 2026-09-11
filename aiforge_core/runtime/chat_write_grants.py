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


def _broad(path: str, home: str) -> bool:
    """``/``, the home directory, or anything ABOVE it (``/Users``, ``/home``)."""
    return path in (os.sep, home) or home.startswith(path.rstrip(os.sep) + os.sep)


def grant_root(target: str) -> "str | None":
    """The folder one approval covers for ``target``: its git repository when
    it is inside one (approving a write to one file of a project means the
    project), else the nearest folder that exists. Never ``/``, the home
    directory or anything above it: a write straight into ``~`` offered to
    grant ``/Users`` — every account's home — for the session. Such a target
    covers only itself; ``None`` means the target IS one of those and can only
    be approved once, never granted."""
    home = os.path.realpath(os.path.expanduser("~"))
    t = os.path.realpath(target)
    if _broad(t, home):
        return None
    d = t
    while not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    probe = d
    while not _broad(probe, home):
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        probe = os.path.dirname(probe)
    if _broad(d, home):
        # /home/me/newproj/x.py → the folder being created under ~, not ~ —
        # and a file written straight into ~ covers just that file.
        rel = os.path.relpath(t, d).split(os.sep)[0]
        return os.path.join(d, rel)
    return d


__all__ = ["granted", "grant", "forget", "forget_all", "grant_root"]
