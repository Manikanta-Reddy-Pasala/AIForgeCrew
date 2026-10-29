"""The model each chat runs on — the chat's own choice over the global pick.

The chat picker used to write the global ``chat`` role (and ``_default``) in
agent_config.json, so changing the model in one chat changed it in every chat.
A chat now keeps its own pick here; the global pick (Home / Settings, or a
chat that made no choice) stays the default.

Applied where a role is resolved (``agent_config.get``): while a turn runs
for a chat that made a choice, the ``chat`` role and every role that runs on
the global default (the team roles with no model of their own) use the chat's
model. Roles with their own configured model keep it, and an
``AIFORGE_<ROLE>_MODEL`` env pin still wins, as it does over the global pick.

Stored at ``$AIFORGE_CONFIG_DIR/chat-models.json`` as
``{session_id: {"model", "base_url", "provider", "api_key", "insecure_tls"}}``.
A deleted chat's choice goes with it (``forget_session``).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

_LOCK = threading.Lock()
_FIELDS = ("model", "base_url", "provider", "api_key", "insecure_tls")
# (mtime_ns, size) -> data: a role is resolved on every LLM call.
_CACHE: dict = {"key": None, "data": {}}


def _path() -> Path:
    return Path(os.path.expanduser(str(config_dir()))) / "chat-models.json"


def _load() -> dict:
    """Every chat's pick; {} when there is no file or it cannot be read (a
    chat then runs on the global pick, as before)."""
    p = _path()
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _CACHE["key"] == key:
        return _CACHE["data"]
    try:
        data = json.loads(p.read_text())
        data = data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        data = {}
    _CACHE.update(key=key, data=data)
    return data


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_text(str(p), json.dumps(data, indent=1))


def get(session_id) -> "dict | None":
    """The chat's own pick (a copy), or None when it uses the global one."""
    if session_id in (None, ""):
        return None
    row = _load().get(str(session_id))
    if not isinstance(row, dict) or not str(row.get("model") or "").strip():
        return None
    return {k: row.get(k) for k in _FIELDS}


def set_session(session_id, model: str, *, base_url=None, provider=None,
                api_key=None, insecure_tls: bool = False) -> dict:
    """Pin ``model`` for ONE chat. Returns the stored pick."""
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    row = {"model": model, "base_url": base_url or None,
           "provider": provider or None, "api_key": api_key or None,
           "insecure_tls": bool(insecure_tls)}
    with _LOCK:
        data = dict(_load())
        data[str(session_id)] = row
        _save(data)
    return dict(row)


def copy_session(from_id, to_id) -> None:
    """Give ``to_id`` the same pick as ``from_id`` (a side task)."""
    with _LOCK:
        data = dict(_load())
        row = data.get(str(from_id))
        if not isinstance(row, dict):
            return
        data[str(to_id)] = dict(row)
        _save(data)


def forget_session(session_id=None) -> None:
    """Drop one chat's pick, or every chat's when ``session_id`` is None (ids
    restart at 1 after delete-all, so leftovers would leak into new chats)."""
    with _LOCK:
        if session_id is None:
            if _path().exists():
                _save({})
            return
        data = dict(_load())
        if data.pop(str(session_id), None) is not None:
            _save(data)


def current() -> "dict | None":
    """The pick of the chat whose turn is running in THIS context, if any.
    Only the context-bound session: the process-wide env fallback names
    whichever chat ran last, and would put a background job of another chat
    on this chat's model."""
    try:
        from aiforge_core.runtime import request_context
        sid = request_context.context_session_id()
    except Exception:  # noqa: BLE001
        return None
    return get(sid) if sid else None


def apply(role: str, row: dict, default_row: "dict | None") -> dict:
    """``row`` (the resolved config for ``role``) with the running chat's pick
    applied: for ``chat`` / ``_default``, and for a role that runs on the
    global default. An env pin for the role keeps winning."""
    pick = current()
    if not pick:
        return row
    if (os.environ.get(f"AIFORGE_{role.upper()}_MODEL") or "").strip():
        return row
    if role not in ("chat", "_default"):
        gd = default_row or {}
        inherits = bool(gd.get("model")) and row.get("model") == gd.get("model") \
            and (row.get("base_url") or None) == (gd.get("base_url") or None)
        if not inherits:
            return row
    out = dict(row)
    out["model"] = pick["model"]
    if pick.get("provider"):
        out["provider"] = pick["provider"]
    same = (pick.get("base_url") or "").rstrip("/").lower() == \
        (row.get("base_url") or "").rstrip("/").lower()
    if pick.get("base_url") and not same:
        out["base_url"] = pick["base_url"]
        # A key belongs to its endpoint: never send this row's key to the
        # chat's (other) server.
        out["api_key"] = pick.get("api_key")
        out["insecure_tls"] = bool(pick.get("insecure_tls"))
    elif pick.get("api_key"):
        out["api_key"] = pick["api_key"]
    return out


__all__ = ["apply", "copy_session", "current", "forget_session", "get",
           "set_session"]
