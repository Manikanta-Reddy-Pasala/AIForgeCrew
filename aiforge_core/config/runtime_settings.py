"""Operator-tunable runtime knobs, persisted + UI-editable.

Two global LLM knobs the operator chooses (NO hardcoded constant wins
over an explicit choice):

* ``max_output_tokens`` — generation cap sent to the model. Too small
  truncates a doer's file-write tool-call args mid-string; too large
  wastes budget. Default 32768.
* ``context_window``    — assumed input context window (tokens). Feeds
  the router's escalation/threshold sizing and is surfaced so the
  operator can match it to whatever the served model actually allows.
  Default 131072.

Resolution order for each knob (first that yields a value wins):
  1. ``runtime_settings.json`` (this store — the UI writes here)
  2. the documented env var (back-compat / headless override)
  3. the built-in default below

Storage: ``$AIFORGE_CONFIG_DIR/runtime_settings.json`` (default
``~/.aiforge``). Kept in its OWN file so it never interferes with the
per-role ``agent_config.json`` load/merge logic.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("aiforge.runtime_settings")

# knob -> (env var consulted when the store has no value, built-in default)
_SPEC: dict[str, tuple[str, int]] = {
    "max_output_tokens": ("AIFORGE_LLM_MAX_TOKENS", 32768),
    "context_window": ("AIFORGE_LOCAL_CTX_WINDOW", 131072),
    # 0/1 flag: force-treat the chat model as vision-capable (for a self-hosted
    # multimodal model the allowlist doesn't recognise). Auto-detection by model
    # id still applies when this is 0.
    "vision_capable": ("AIFORGE_CHAT_VISION_CAPABLE", 0),
    # 0/1 "cave mode": send the agents the leanest useful context — smaller repo
    # map, skip optional skills/workflows/mentions blocks, fewer memory hits,
    # tighter condense budget, harder prompt compression. Cheaper + faster on a
    # small local model; the agent can still grep/read on demand.
    "cave_mode": ("AIFORGE_CAVE_MODE", 0),
}

# Sanity bounds — reject obviously-bad values from the API/UI so a typo
# can't wedge the pipeline (e.g. 0 or a negative cap).
_BOUNDS: dict[str, tuple[int, int]] = {
    "max_output_tokens": (256, 1_000_000),
    "context_window": (1024, 10_000_000),
    "vision_capable": (0, 1),
    "cave_mode": (0, 1),
}


def _path() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "runtime_settings.json"


from aiforge_core.config import _filecache as _fc


def _read_store() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = _fc.read_json(p)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("runtime_settings.json unreadable: %s", exc)
        return {}


def get(name: str) -> int:
    """Resolve a knob: stored value → env var → built-in default."""
    if name not in _SPEC:
        raise KeyError(name)
    env_var, default = _SPEC[name]
    stored = _read_store().get(name)
    if isinstance(stored, int) and stored > 0:
        return stored
    raw = os.environ.get(env_var)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return default


def all_settings() -> dict[str, int]:
    """Current resolved value of every knob (for the GET endpoint)."""
    return {name: get(name) for name in _SPEC}


def set_many(values: dict[str, Any]) -> dict[str, int]:
    """Persist the given knobs (only recognised, in-bounds keys). Returns
    the full resolved settings afterwards. Raises ValueError on a bad
    value so the API can surface a 400."""
    store = _read_store()
    for name, val in values.items():
        if name not in _SPEC:
            continue
        try:
            ival = int(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer")
        lo, hi = _BOUNDS[name]
        if not (lo <= ival <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
        store[name] = ival
    _path().write_text(json.dumps(store, indent=2))
    return all_settings()


__all__ = ["get", "all_settings", "set_many"]
