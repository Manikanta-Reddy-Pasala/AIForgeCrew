"""User-managed model registry — the simplified Settings flow.

The user adds one or two models (each = an OpenAI-compatible endpoint: a model
id + base URL + optional API key + TLS + vision flag) ONCE, here. Every agent
then just *picks* a model by name — no per-agent URLs/keys. Applying a model to
a role writes that model's connection details into the role's agent_config via
``agent_config.set_role``.

Stored as JSON at ``$AIFORGE_CONFIG_DIR/model_registry.json``. API keys are kept
server-side and never returned (only ``api_key_set``).
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

_LOCK = threading.Lock()
_VISION = ("auto", "yes", "no")


def _path() -> str:
    root = os.path.expanduser(os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge"))
    return os.path.join(root, "model_registry.json")


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return []


def _save(rows: list[dict]) -> None:
    p = _path()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    os.replace(tmp, p)


def _slug(label: str, model: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (label or model or "model").lower()).strip("-")
    return base or "model"


def _public(row: dict) -> dict:
    """Registry row without the raw key."""
    return {"id": row.get("id"), "label": row.get("label") or row.get("model"),
            "model": row.get("model"), "base_url": row.get("base_url") or "",
            "insecure_tls": bool(row.get("insecure_tls")),
            "vision": row.get("vision") or "auto",
            "api_key_set": bool(row.get("api_key"))}


def list_models() -> list[dict]:
    return [_public(r) for r in _load()]


def get_model(model_id: str) -> dict | None:
    for r in _load():
        if r.get("id") == model_id:
            return r
    return None


def add_model(*, label: str, model: str, base_url: str = "",
              api_key: str | None = None, insecure_tls: bool = False,
              vision: str = "auto") -> dict:
    model = (model or "").strip()
    if not model:
        raise ValueError("model id is required")
    if vision not in _VISION:
        vision = "auto"
    with _LOCK:
        rows = _load()
        mid = _slug(label, model)
        existing = {r["id"] for r in rows}
        uid, n = mid, 2
        while uid in existing:
            uid = f"{mid}-{n}"
            n += 1
        row = {"id": uid, "label": (label or model).strip(), "model": model,
               "base_url": (base_url or "").strip(), "api_key": api_key or "",
               "insecure_tls": bool(insecure_tls), "vision": vision}
        rows.append(row)
        _save(rows)
        return _public(row)


def update_model(model_id: str, **fields: Any) -> dict | None:
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("id") != model_id:
                continue
            for k in ("label", "model", "base_url"):
                if fields.get(k) is not None:
                    r[k] = str(fields[k]).strip()
            if fields.get("insecure_tls") is not None:
                r["insecure_tls"] = bool(fields["insecure_tls"])
            if fields.get("vision") in _VISION:
                r["vision"] = fields["vision"]
            # Only overwrite the key when a non-empty one is supplied.
            if fields.get("api_key"):
                r["api_key"] = fields["api_key"]
            _save(rows)
            return _public(r)
    return None


def remove_model(model_id: str) -> bool:
    with _LOCK:
        rows = _load()
        new = [r for r in rows if r.get("id") != model_id]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def vision_for(model: str, base_url: str = "") -> str | None:
    """Explicit vision flag ('yes'/'no') for a model matched by id+url, or None
    when unset/auto — so callers can fall back to probing."""
    model = (model or "").strip()
    for r in _load():
        if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
            v = r.get("vision") or "auto"
            return v if v in ("yes", "no") else None
    return None


def apply_to_roles(model_id: str, roles: list[str]) -> dict:
    """Point each role at this registry model (writes its connection details into
    agent_config). Returns ``{applied: [...], errors: {...}}``."""
    row = get_model(model_id)
    if row is None:
        raise ValueError(f"unknown model: {model_id}")
    from aiforge_core.config import agent_config
    applied, errors = [], {}
    for role in roles:
        try:
            agent_config.set_role(
                role, "openai_compatible", row["model"],
                base_url=row.get("base_url") or None,
                api_key=row.get("api_key") or None,
                insecure_tls=bool(row.get("insecure_tls")))
            applied.append(role)
        except Exception as exc:  # noqa: BLE001
            errors[role] = str(exc)
    return {"applied": applied, "errors": errors}


__all__ = ["list_models", "get_model", "add_model", "update_model",
           "remove_model", "vision_for", "apply_to_roles"]
