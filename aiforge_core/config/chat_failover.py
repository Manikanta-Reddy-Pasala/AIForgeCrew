"""The models a chat turn fails over to when its model stops answering.

The owner configures several models in Settings; when the one a chat runs on
goes silent ("no output for 4 minutes") the turn used to wait it out, re-send
the same request to the same dead model, then wait for it to come back —
forever by default. This is the ordered list (1-3 models, by registry id) the
turn switches to instead, plus the two thresholds (llm/model_failover):

* ``stall_s`` — no first token / no new token for this long ends the request
  on the current model (``AIFORGE_LLM_FAILOVER_STALL_S``, default 180);
* ``fails``   — this many transport failures in a row (refused, reset, 5xx,
  read timeout) on the current model end it too (``AIFORGE_LLM_FAILOVER_FAILS``,
  default 2).

A saved value beats the env var (as runtime_settings does); the env beats the
built-in default. ``AIFORGE_LLM_FAILOVER=0`` turns the whole thing off whatever
is saved. No list = no failover: nothing is chosen for the owner.

Stored at ``$AIFORGE_CONFIG_DIR/chat-failover.json`` as
``{"models": [registry_id, ...], "stall_s": int|null, "fails": int|null}``.
Only registry ids are kept: the registry row is the connection (URL, key,
TLS), so a key is never copied here.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

MAX_MODELS = 3
STALL_DEFAULT_S = 180
FAILS_DEFAULT = 2
#: (low, high) for the two thresholds — a typo must not make every request
#: fail over after one second, or never.
STALL_BOUNDS = (30, 3600)
FAILS_BOUNDS = (1, 20)

_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.path.expanduser(str(config_dir()))) / "chat-failover.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except Exception:  # noqa: BLE001 — missing / unreadable: nothing configured
        return {}
    return data if isinstance(data, dict) else {}


def enabled_by_env() -> bool:
    """The off switch: ``AIFORGE_LLM_FAILOVER=0`` (or false/no/off)."""
    return (os.environ.get("AIFORGE_LLM_FAILOVER") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def model_ids() -> list[str]:
    """The saved list, in order (registry ids)."""
    raw = _load().get("models")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x or "").strip()][:MAX_MODELS]


def rows() -> list[dict]:
    """The raw registry rows of the list, in order. A row removed from the
    registry since is skipped, not an error."""
    from aiforge_core.config import model_registry
    out = []
    for mid in model_ids():
        row = model_registry.get_model(mid)
        if isinstance(row, dict) and str(row.get("model") or "").strip():
            out.append(dict(row))
    return out


def _bounded(stored, env: str, default: int, bounds) -> int:
    lo, hi = bounds
    for raw in (stored, os.environ.get(env)):
        if raw in (None, ""):
            continue
        try:
            return max(lo, min(hi, int(float(raw))))
        except (TypeError, ValueError):
            continue
    return default


def stall_s() -> int:
    """Seconds of silence (no first token / no new token) before failing over."""
    return _bounded(_load().get("stall_s"), "AIFORGE_LLM_FAILOVER_STALL_S",
                    STALL_DEFAULT_S, STALL_BOUNDS)


def fails() -> int:
    """Transport failures in a row on one model before failing over."""
    return _bounded(_load().get("fails"), "AIFORGE_LLM_FAILOVER_FAILS",
                    FAILS_DEFAULT, FAILS_BOUNDS)


def view() -> dict:
    """What Settings shows: the list, the effective thresholds, the switch."""
    data = _load()
    return {"models": model_ids(), "stall_s": stall_s(), "fails": fails(),
            "stall_s_saved": data.get("stall_s"), "fails_saved": data.get("fails"),
            "enabled": enabled_by_env(), "max_models": MAX_MODELS}


def save(models: list, stall: "int | None" = None, fail_n: "int | None" = None) -> dict:
    """Replace the list (and the thresholds; None = use env / default).
    Raises ValueError on an unknown id, a duplicate or more than MAX_MODELS."""
    from aiforge_core.config import model_registry
    ids = [str(m or "").strip() for m in (models or [])]
    ids = [m for m in ids if m]
    if len(ids) > MAX_MODELS:
        raise ValueError(f"at most {MAX_MODELS} failover models")
    if len(set(ids)) != len(ids):
        raise ValueError("a model is listed twice")
    for mid in ids:
        if model_registry.get_model(mid) is None:
            raise ValueError(f"unknown model id {mid!r} (add it under Models first)")
    for val, (lo, hi), name in ((stall, STALL_BOUNDS, "stall_s"),
                                (fail_n, FAILS_BOUNDS, "fails")):
        if val is not None and not (lo <= int(val) <= hi):
            raise ValueError(f"{name} must be between {lo} and {hi}")
    with _LOCK:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic.write_text(str(p), json.dumps(
            {"models": ids, "stall_s": None if stall is None else int(stall),
             "fails": None if fail_n is None else int(fail_n)}, indent=1))
    return view()


__all__ = ["FAILS_BOUNDS", "MAX_MODELS", "STALL_BOUNDS", "enabled_by_env", "fails",
           "model_ids", "rows", "save", "stall_s", "view"]
