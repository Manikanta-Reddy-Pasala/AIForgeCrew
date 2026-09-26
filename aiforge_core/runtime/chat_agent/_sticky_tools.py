"""Tool families a chat session has already used stay on its native list.

A follow-up such as "move ONE-356 to Done" may not name Jira, and the
message that did may have been compacted away. The families and tools a
session enabled are kept here, per session, so later turns start with them.

One small JSON file in the config dir, like ``chat_write_grants``, so it
survives an API restart. Soft-fail: an unreadable file means nothing is kept.
"""
from __future__ import annotations

import json
import os
import threading

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()
_MAX_NAMES = 64


def _path() -> str:
    return os.path.join(str(config_dir()), "chat-tool-families.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt → nothing kept
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        _atomic.write_text(_path(), json.dumps(data, indent=1))
    except Exception:  # noqa: BLE001 — the in-turn list still applies
        pass


def kept(session_id) -> set[str]:
    """Family and tool names this session enabled before."""
    if session_id is None:
        return set()
    with _LOCK:
        names = _load().get(str(session_id)) or []
    return {n for n in names if isinstance(n, str) and n}


def keep(session_id, names) -> None:
    """Add ``names`` to the session's list. Writes only when it grows."""
    if session_id is None:
        return
    new = [n for n in (names or ()) if isinstance(n, str) and n]
    if not new:
        return
    with _LOCK:
        data = _load()
        have = list(data.get(str(session_id)) or [])
        grown = [n for n in dict.fromkeys(new) if n not in have]
        if not grown:
            return
        data[str(session_id)] = (have + grown)[-_MAX_NAMES:]
        _save(data)


def forget(session_id=None) -> None:
    """Drop one session's list, or every list when ``session_id`` is None."""
    with _LOCK:
        if session_id is None:
            _save({})
            return
        data = _load()
        if data.pop(str(session_id), None) is not None:
            _save(data)
