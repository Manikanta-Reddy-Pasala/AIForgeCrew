"""Persisted integration settings (Confluence, …) configured from the UI.

Stored as ``$AIFORGE_CONFIG_DIR/security/integrations.json`` (the 0700
credential folder — see ``config.secure_store``)
— the same place per-role agent config lives. Env vars always WIN over the
stored value at read time, so an operator can still override via ``.env`` /
systemd without touching the UI.
"""
from __future__ import annotations

import json
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir


def _path() -> Path:
    """The tokens file, inside the 0700 ``security/`` folder (see
    ``config.secure_store``); a legacy copy in the config root is moved there
    on first use."""
    from aiforge_core.config.secure_store import secure_path
    d = Path(str(config_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return secure_path("integrations.json")


def load_all() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text()) or {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def get(name: str) -> dict:
    val = load_all().get(name)
    return val if isinstance(val, dict) else {}


def set_(name: str, cfg: dict) -> dict:
    """Merge ``cfg`` into the stored entry for ``name`` (None values skipped,
    so an omitted secret is preserved). Returns the saved entry."""
    data = load_all()
    cur = data.get(name) if isinstance(data.get(name), dict) else {}
    cur.update({k: v for k, v in cfg.items() if v is not None})
    data[name] = cur
    # Atomic publish — a crash mid-write, or a second process saving another
    # integration at the same moment, must not leave a truncated or blended
    # integrations.json that loses every saved credential.
    _atomic.write_text(_path(), json.dumps(data, indent=2))
    return cur


__all__ = ["load_all", "get", "set_"]
