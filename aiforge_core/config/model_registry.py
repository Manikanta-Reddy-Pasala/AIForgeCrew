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

import copy
import json
import os
import re
import threading
from typing import Any

from aiforge_core.config import _atomic
from aiforge_core.config.paths import config_dir

from ._model_context import (  # noqa: F401  # re-exported
    _CTX_CEILING,
    _CTX_STATIC_DEFAULT,
    _autodetect_ctx_enabled,
    _autodetected_window,
    _explicit_global_window,
    _explicit_role_window,
    context_window_for_role,
    context_window_source,
    effective_context_window,
    parallel_for,
    thinking_for,
    vision_for,
)
from ._model_roles import (  # noqa: F401  # re-exported
    _CODER_ROLES,
    _FAST_ROLES,
    _NON_GENERATIVE_MARKERS,
    _THINKING_ROLES,
    _assign_role,
    _by_ctx,
    _is_generative,
    auto_assign,
    is_fast_role,
    suggest_assignments,
)

_LOCK = threading.Lock()
_VISION = ("auto", "yes", "no")
_THINKING = ("auto", "yes", "no")

# Name heuristics for auto-detecting a reasoning/"thinking" model (emits a
# <think> channel). Used when thinking=='auto'. Substring match, lowercased.
_THINKING_MARKERS = (
    "qwythos", "ornith", "thinking", "reasoner", "reasoning", "-r1", "r1-",
    "deepseek-r1", "qwq", "o1", "o3", "o4-mini", "marco-o1", "sky-t1", "-think",
)
# NOTE: markers are substring-matched, so a bare "vl" wrongly flagged "vllm" /
# "nvl" etc. — use BOUNDARY forms (-vl / vl- / -vl-) so a served-by-vllm text
# model isn't mistaken for a vision-language model.
# Substring markers for a VLM NAME. Deliberately conservative — a name-heuristic
# hit is PERSISTED as vision WITHOUT probing, so a false match durably mis-marks
# a text model. Dropped: 'gemma-3'/'gemma3' (the family mixes text-only 1b with
# vision 4b+ under one prefix — let the live probe decide), and '-v-' (matched
# version tags like 'foo-v-2'). Vision-only sizes stay explicit.
_VISION_MARKERS = (
    "-vl", "vl-", "-vl-", "vision", "llava", "bakllava", "moondream",
    "pixtral", "internvl", "minicpm-v", "qwen2-vl", "qwen2.5-vl",
    "gemma-3-4b", "gemma-3-12b", "gemma-3-27b",   # vision gemma sizes only
    "llama-3.2-11b", "llama-3.2-90b", "-omni", "cogvlm", "nvlm",
)


def detect_capability(model_id: str, kind: str) -> bool:
    """Heuristic capability detection from the model id when the flag is 'auto'.
    kind = 'thinking' | 'vision'. Substring match on markers."""
    m = (model_id or "").lower()
    markers = _THINKING_MARKERS if kind == "thinking" else _VISION_MARKERS
    return any(k in m for k in markers)


def _path() -> str:
    root = str(config_dir())
    return os.path.join(root, "model_registry.json")


# (path, mtime_ns, size) -> parsed rows. Every chat step asks for the
# window several times; re-reading and re-parsing the file each time was
# measurable on a turn of many fast tool steps. A write changes the stamp.
_CACHE: dict = {"stamp": None, "rows": []}
_CACHE_LOCK = threading.Lock()


def _stamp(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return (path, None, None, None)
    # The inode too: an atomic rename changes it even when a coarse mtime
    # and the size do not.
    return (path, st.st_mtime_ns, st.st_size, st.st_ino)


def _load() -> list[dict]:
    """The registry rows. A private copy each call: callers edit and save."""
    path = _path()
    stamp = _stamp(path)
    with _CACHE_LOCK:
        if _CACHE["stamp"] == stamp and stamp[1] is not None:
            return copy.deepcopy(_CACHE["rows"])
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        rows = []
    with _CACHE_LOCK:
        _CACHE["stamp"], _CACHE["rows"] = stamp, rows
    return copy.deepcopy(rows)


def _save(rows: list[dict]) -> None:
    _atomic.write_text(_path(), json.dumps(rows, indent=2))
    with _CACHE_LOCK:
        _CACHE["stamp"] = None


def _slug(label: str, model: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (label or model or "model").lower()).strip("-")
    return base or "model"


def _resolve(flag: str, model_id: str, kind: str) -> bool:
    """Flag ('auto'|'yes'|'no') → effective bool. 'auto' → name heuristic."""
    f = (flag or "auto").lower()
    if f == "yes":
        return True
    if f == "no":
        return False
    return detect_capability(model_id, kind)


def _public(row: dict) -> dict:
    """Registry row without the raw key, with resolved capabilities."""
    mid = row.get("model") or ""
    vision = row.get("vision") or "auto"
    thinking = row.get("thinking") or "auto"
    return {"id": row.get("id"), "label": row.get("label") or row.get("model"),
            "model": row.get("model"), "base_url": row.get("base_url") or "",
            "insecure_tls": bool(row.get("insecure_tls", True)),
            "vision": vision, "thinking": thinking,
            # resolved booleans so the UI shows a badge + auto-select can match
            "has_vision": _resolve(vision, mid, "vision"),
            "has_thinking": _resolve(thinking, mid, "thinking"),
            "context_window": int(row.get("context_window") or 0),
            "parallel": int(row.get("parallel") or 0),
            "api_key_set": bool(row.get("api_key"))}


def list_models() -> list[dict]:
    return [_public(r) for r in _load()]


def _chain_candidate(r, want: str, want_host: str) -> bool:
    """Whether registry row ``r`` is a usable fallback for the failed
    ``(want, want_host)`` model.

    A hand-edited registry can hold anything, so a non-dict / model-less row is
    skipped. Exclusion is by CONNECTION, not model id: the same model on a
    second box is the textbook redundancy setup, so only the row that IS the
    failed endpoint is skipped (a row with no base_url resolves to the failed
    host, so it counts as the same endpoint). Non-generative rows (embed /
    reranker / gte / e5) are dropped so they don't burn a round trip."""
    if not isinstance(r, dict):
        return False
    mid = str(r.get("model") or "").strip()
    if not mid:
        return False
    row_host = str(r.get("base_url") or "").strip().rstrip("/").lower() or want_host
    if mid.lower() == want and row_host == want_host:
        return False
    return _is_generative(mid)


def chain_after(model: str, base_url: str = "") -> list[dict]:
    """The OTHER configured models, in registry order, as raw rows.

    "I added four models; when the one chat picked stops answering it should try
    the others" — the registry was only ever a selection list, so a dead model
    was the end of the road on a single-provider install (the provider fallback
    chain is CLOUD escalation, empty with no cloud key). Raw rows (api_key
    included) because the caller builds an Endpoint from them.
    """
    want = (model or "").strip().lower()
    want_host = (base_url or "").strip().rstrip("/").lower()
    return [dict(r) for r in _load() if _chain_candidate(r, want, want_host)]


def get_model(model_id: str) -> dict | None:
    for r in _load():
        if r.get("id") == model_id:
            return r
    return None


def add_model(*, label: str, model: str, base_url: str = "",
              api_key: str | None = None, insecure_tls: bool = True,
              vision: str = "auto", thinking: str = "auto",
              context_window: int = 0) -> dict:
    model = (model or "").strip()
    if not model:
        raise ValueError("model id is required")
    if vision not in _VISION:
        vision = "auto"
    if thinking not in _THINKING:
        thinking = "auto"
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
               "insecure_tls": bool(insecure_tls), "vision": vision,
               "thinking": thinking,
               "context_window": max(0, int(context_window or 0))}
        rows.append(row)
        _save(rows)
        return _public(row)


def _apply_model_fields(r: dict, fields: dict) -> None:
    """Apply the updatable fields onto registry row ``r`` in place, each with its
    own validation/coercion. A key is overwritten only when a non-empty one is
    supplied."""
    for k in ("label", "model", "base_url"):
        if fields.get(k) is not None:
            r[k] = str(fields[k]).strip()
    if fields.get("insecure_tls") is not None:
        r["insecure_tls"] = bool(fields["insecure_tls"])
    if fields.get("vision") in _VISION:
        r["vision"] = fields["vision"]
    if fields.get("thinking") in _THINKING:
        r["thinking"] = fields["thinking"]
    if fields.get("context_window") is not None:
        r["context_window"] = max(0, int(fields["context_window"] or 0))
    if fields.get("parallel") is not None:
        r["parallel"] = max(0, int(fields["parallel"] or 0))
    if fields.get("api_key"):
        r["api_key"] = fields["api_key"]


def update_model(model_id: str, **fields: Any) -> dict | None:
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("id") != model_id:
                continue
            _apply_model_fields(r, fields)
            _save(rows)
            return _public(r)
    return None


def connection_for(model: str, base_url: str = "") -> dict | None:
    """The CONNECTION the user registered for ``model`` — its own endpoint, not
    somebody else's.

    Each registry row carries its own ``base_url``/key/TLS, but the paths that
    only pass a MODEL ID around (picking a chat model, a role row saved without
    a URL) used to fall back to whatever endpoint was already configured — so
    selecting a model added from a SECOND server silently kept the FIRST
    server's URL and every call went to a host that had never heard of it.

    Returns ``{"base_url", "api_key", "insecure_tls"}``, or None when:
      * the registry has no row for this model (the caller's own fallback is
        then correct — it may be an env-pinned model that was never added), or
      * several rows share the model id and ``base_url`` doesn't say which
        (the same id served from two endpoints is exactly the case that must
        not be guessed).
    A row registered WITHOUT a base_url yields None too: it has no endpoint of
    its own to prefer.
    """
    model = (model or "").strip()
    if not model:
        return None
    want = (base_url or "").strip()
    with _LOCK:
        rows = [r for r in _load()
                if (r.get("model") or "") == model
                and (not want or (r.get("base_url") or "") == want)]
    if len(rows) != 1:
        return None
    row = rows[0]
    if not (row.get("base_url") or "").strip():
        return None
    return {"base_url": row["base_url"].strip(),
            "api_key": row.get("api_key") or None,
            "insecure_tls": bool(row.get("insecure_tls"))}


def set_vision_flag(model: str, base_url: str, flag: str) -> bool:
    """Persist a resolved vision flag (``yes``/``no``) onto the row matched by
    model id (+ base_url when given). Used by the auto-detect path to make a
    probed/heuristic result durable so it survives a restart and shows the right
    badge. No-op (returns False) when ``flag`` is invalid or no row matches (an
    env-override model that isn't a registry row keeps only the in-memory cache)."""
    if flag not in ("yes", "no"):
        return False
    model = (model or "").strip()
    if not model:
        return False
    with _LOCK:
        rows = _load()
        for r in rows:
            if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
                if r.get("vision") == flag:
                    return True
                r["vision"] = flag
                _save(rows)
                return True
    return False


def remove_model(model_id: str) -> bool:
    with _LOCK:
        rows = _load()
        new = [r for r in rows if r.get("id") != model_id]
        if len(new) == len(rows):
            return False
        _save(new)
        return True


def context_for(model: str, base_url: str = "") -> int:
    """Per-model context window (tokens) for a model matched by id+url, or 0
    when unset (caller falls back to the global setting)."""
    model = (model or "").strip()
    for r in _load():
        if r.get("model") == model and (not base_url or r.get("base_url") == base_url):
            return int(r.get("context_window") or 0)
    return 0


def sync_from_config() -> dict:
    """Seed the registry from the agents' CURRENT per-role config — so a fresh
    registry isn't empty when models are already wired (e.g. via the legacy flow
    or env). Adds each distinct (model, base_url) that isn't registered yet.
    Returns ``{added: [ids], count}``."""
    try:
        from aiforge_core.config import agent_config
        cfg = agent_config.load_all()
    except Exception:  # noqa: BLE001
        return {"added": [], "count": 0}
    have = {(r.get("model"), r.get("base_url") or "") for r in _load()}
    added: list[str] = []
    for _role, c in cfg.items():
        model = (c.get("model") or "").strip()
        if not model or model.startswith("local-model-unconfigured"):
            continue
        key = (model, (c.get("base_url") or "").strip())
        if key in have:
            continue
        have.add(key)
        row = add_model(label=model.split("/")[-1], model=model,
                        base_url=c.get("base_url") or "", api_key=c.get("api_key"),
                        insecure_tls=bool(c.get("insecure_tls", True)))
        added.append(row["id"])
    return {"added": added, "count": len(added)}


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


__all__ = ["list_models", "get_model", "chain_after", "add_model",
           "connection_for",
           "update_model", "remove_model", "vision_for", "apply_to_roles",
           "detect_capability", "suggest_assignments", "auto_assign"]
