"""Chat and orchestrator model pickers: served-model discovery, selection,
and reload."""
from __future__ import annotations

import os

from fastapi import HTTPException
from pydantic import BaseModel, Field

from aiforge_core.config import agent_config as _acfg
from aiforge_core.config import model_registry as _model_registry

from ._core import (
    router,
)


def _served_model_ids(provider: str) -> set:
    """IDs the provider is currently serving (active/loaded). For local /
    ollama_cloud this hits /v1/models; empty set when undiscoverable."""
    try:
        return {m.get("id") for m in (_acfg.list_models(provider) or [])
                if m.get("id")}
    except Exception:
        return set()


def _served_model_ids_for_role(role: str) -> set:
    """Served model IDs for a specific role's endpoint.

    openai_compatible has no static catalog — discover by probing the
    role's configured base_url (with its api_key + TLS settings) /models,
    exactly like the home-page Test. Falls back to provider-level
    discovery for local / ollama_cloud.
    """
    try:
        rl = _acfg.resolve_litellm(role)
    except Exception:
        rl = {}
    provider = (_acfg.get(role) or {}).get("provider") or "local"
    if provider == "openai_compatible":
        try:
            from aiforge_core.llm.providers.openai_compatible import probe
            res = probe(rl.get("api_base") or "", rl.get("api_key"),
                        insecure=bool(rl.get("insecure_tls")))
            return set(res.get("models") or [])
        except Exception:
            return set()
    return _served_model_ids(provider)


def _model_env_override(role: str) -> dict | None:
    """If an ``AIFORGE_<ROLE>_MODEL`` env var is set, it ALWAYS wins over the
    picker's persisted value (agent_config.load_all ops escape hatch). Return
    the pinning var + value so the UI can WARN that a pick won't take effect —
    otherwise the picker silently saves a model that never runs."""
    var = f"AIFORGE_{role.upper()}_MODEL"
    val = os.environ.get(var)
    if val and val.strip():
        return {"var": var, "model": val.strip()}
    return None


def _chat_capable(mid: str) -> bool:
    """Embedding models are not chat-capable."""
    return bool(mid) and "embed" not in mid.lower()


def _url_key(url: "str | None") -> str:
    """Comparable form of a base_url (trailing slash / case are not identity)."""
    return (url or "").strip().rstrip("/").lower()


def _registry_models(served: "set | list", current_url: str = "") -> dict:
    """Models the user CONFIGURED, keyed by (model id, ITS OWN base_url).

    Keyed by the pair, not the id: the same model id registered against two
    servers is TWO entries, and collapsing them on the id hid the second
    registration completely — it could not be picked, so "use the copy on the
    other host" was not expressible. Each entry carries its own ``base_url`` so
    the pick can say which endpoint it means.

    ``active`` is only meaningful for rows on the endpoint the served list came
    from: an id served by host A says nothing about the same id on host B.
    Optional — an unavailable registry just means the served list stands alone.
    """
    out: dict = {}
    try:
        from aiforge_core.config import model_registry
        rows = model_registry.list_models()
    except Exception:  # noqa: BLE001 — registry optional; fall back to served
        return out
    for r in rows:
        mid = (r.get("model") or "").strip()
        url = (r.get("base_url") or "").strip()
        key = (mid, _url_key(url))
        if not _chat_capable(mid) or key in out:
            continue
        same_endpoint = not url or _url_key(url) == _url_key(current_url)
        out[key] = {"id": mid, "base_url": url,
                    "label": (r.get("label") or mid.split("/")[-1]),
                    "active": same_endpoint and mid in served}
    return out


def _merge_registry_and_served(served: "set | list",
                               current_url: str = "") -> list:
    """Configured models UNION currently-served, active-first then by id.

    Split out of `chat_models`, which was building this list AND assembling the
    response around it — two jobs, and only this one has any logic in it.
    """
    out = _registry_models(served, current_url)
    for mid in served:
        if _chat_capable(mid) and (mid, _url_key(current_url)) not in out:
            out[(mid, _url_key(current_url))] = {
                "id": mid, "base_url": current_url or "",
                "label": mid.split("/")[-1], "active": True}
    return sorted(out.values(),
                  key=lambda m: (not m["active"], m["id"], m["base_url"]))


@router.get("/api/chat/models")
def chat_models() -> dict:
    """Models the user can pick for the 'chat' slot.

    Lists every model the user CONFIGURED (the model registry — the portable,
    machine-agnostic "models I added" surface managed in Settings) UNIONed with
    whatever the provider is currently serving, and flags each ``active`` =
    currently loaded. This is deliberately NOT loaded-only: a local model host
    (LM Studio) exposes only *loaded* models over HTTP, so listing served-only
    hides every model the user added but hasn't loaded. Selection does not load
    anything — that stays out of the UI; it only sets which model chat uses.
    Embedding models are excluded (not chat-capable). Provider-generic; no
    host-specific discovery.
    """
    row = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = row.get("provider") or "local"
    served = _served_model_ids_for_role("chat")
    current = row.get("model")

    models = _merge_registry_and_served(served, row.get("base_url") or "")
    # An env pin (AIFORGE_CHAT_MODEL / AIFORGE_DEFAULT_MODEL) overrides the
    # picker — surface it so the UI can show "env-pinned, picking won't apply".
    env_ovr = _model_env_override("chat") or _model_env_override("_default")
    return {
        "provider": provider,
        "current": current,
        # WHICH copy is selected. With the same id registered against two
        # servers, the id alone cannot say, and the picker would show the wrong
        # row as current.
        "current_base_url": row.get("base_url") or "",
        "current_active": (current in served) if served else True,
        "models": models,
        "env_override": env_ovr,
    }


class _ChatModelBody(BaseModel):
    model: str = Field(..., min_length=1)
    # WHICH copy of that model. The same id can be registered against two
    # servers; without this the pick is ambiguous and the endpoint can only be
    # guessed — which is how a model on a second host kept being called on the
    # first. The picker sends back the base_url it listed.
    base_url: str | None = Field(None)
    provider: str | None = Field(None)
    apply_all: bool = Field(True, description="also set the global _default so "
                            "TEAM mode (all agents) uses this model")


def _endpoint_for_picked_model(model: str, cur: dict,
                               want_url: str = "") -> tuple:
    """``(base_url, api_key, insecure_tls)`` for the model being picked.

    The model's OWN endpoint wins. Models are registered one by one, each with
    its own base_url, so a model added from a second server must be called on
    that server: carrying the chat slot's CURRENT base_url across a model change
    sent every request for the new model to the previous model's host, which
    answers 404/400 for an id it has never served. The slot's current connection
    is the fallback only when the registry has no row that says otherwise — an
    env-pinned model, one registered without a URL, or an id served from two
    endpoints (which must not be guessed between).

    ``api_key`` of None is meaningful: ``set_role`` keeps the stored key.
    """
    conn = _model_registry.connection_for(model, want_url) or {}
    # An explicit pick stands even when the registry can't confirm it: the
    # caller named the endpoint, so honour it rather than falling back to the
    # slot's current one.
    return (conn.get("base_url") or want_url or cur.get("base_url"),
            conn.get("api_key"),
            bool(conn.get("insecure_tls") if conn else cur.get("insecure_tls")))


def _env_pin_warning(cfg: dict, apply_all: bool) -> tuple:
    """``(env_override, warning)`` for a pick an env var will overrule.

    An AIFORGE_<ROLE>_MODEL pin wins on READ, so the value is persisted but the
    running model does not change — without saying so, the picker looks like it
    silently no-ops.
    """
    env_ovr = _model_env_override("chat")
    if apply_all and not env_ovr:
        env_ovr = _model_env_override("_default")
    if env_ovr and env_ovr.get("model") != cfg.get("model"):
        return env_ovr, (
            f"saved, but {env_ovr['var']}={env_ovr['model']} is set and "
            "overrides it — the running model won't change until that "
            "env var is unset")
    return env_ovr, None


@router.put("/api/chat/model", responses={400: {"description": "Bad request"}})
def chat_model_set(body: _ChatModelBody) -> dict:
    """Persist the chat slot's model + report whether it's active (served
    right now). Rejected only on bad input — an inactive model is saved
    but flagged so the UI can warn."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    _base, _key, _tls = _endpoint_for_picked_model(
        body.model, cur, body.base_url or "")
    try:
        cfg = _acfg.set_role("chat", provider, body.model,
                             base_url=_base, api_key=_key, insecure_tls=_tls)
        # Apply to ALL agents by default: the picked model also becomes the
        # global _default so TEAM mode (triage/planner/doer/…) uses it too —
        # otherwise electing a bigger model only changes single-agent chat.
        if body.apply_all:
            gd = _acfg.get("_default") if "_default" in _acfg.archetypes() else {}
            _acfg.set_role("_default", provider, body.model,
                           base_url=_base or gd.get("base_url"),
                           api_key=_key, insecure_tls=_tls)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    served = _served_model_ids_for_role("chat")
    env_ovr, warning = _env_pin_warning(cfg, body.apply_all)
    # Model changed → re-identify its vision capability (background).
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.reset_vision_cache()
        vision_detect.warm_vision_async("chat")
    except Exception:  # noqa: BLE001
        pass
    return {"provider": cfg.get("provider"), "model": cfg.get("model"),
            "applied_to": "all agents" if body.apply_all else "chat only",
            "active": (cfg.get("model") in served) if served else True,
            "env_override": env_ovr, "warning": warning}


class _ModelReloadBody(BaseModel):
    model: str = Field(..., min_length=1)
    context_length: int = Field(..., ge=1024, le=2_097_152,
                                description="LM Studio --context-length; "
                                "clamped up to the 64K project floor")
    ttl: int = Field(0, ge=0, description="--ttl seconds; 0 = no idle unload")


@router.post("/api/chat/model/reload")
def chat_model_reload(body: _ModelReloadBody) -> dict:
    """(Re)load a model on the LM Studio host at a chosen context window.

    Powers the UI 'context window' control: SSHes to AIFORGE_LMS_HOST,
    unloads any running copy of the model, then ``lms load`` at the
    requested ctx. Blocking until the load returns. 503 when no LMS host
    is configured (e.g. a cloud-only deploy), 502 on SSH/load failure."""
    from aiforge_core.runtime import local_starter as _ls
    res = _ls.load_model_now(body.model, body.context_length, ttl=body.ttl)
    if not res.get("ok"):
        err = res.get("error", "reload failed")
        code = 503 if "AIFORGE_LMS_HOST" in err else 502
        raise HTTPException(code, err)
    # A freshly (re)loaded model is now reachable — identify its vision
    # capability in the background so a definitive probe can persist.
    try:
        from aiforge_core.runtime import vision_detect
        vision_detect.reset_vision_cache()
        vision_detect.warm_vision_async("chat")
    except Exception:  # noqa: BLE001
        pass
    return res


_ORCHESTRATOR_ROLES = ("enhancer", "architect", "planner")


@router.get("/api/chat/orchestrator-model")
def orchestrator_model_get() -> dict:
    # The orchestrator picks from the SAME model universe as the worker/chat
    # slot — that's the real multi-model endpoint. Do NOT probe the planner
    # role: its base_url may be a per-model proxy (e.g. /proxy/<model>) that
    # serves one model and returns no /v1/models list, which would empty the
    # dropdown and spam "probe FAILED". Always include the current model so
    # the dropdown never renders empty.
    row = _acfg.get("planner") if "planner" in _acfg.archetypes() else {}
    served = set(_served_model_ids_for_role("chat"))
    current = row.get("model")
    if current:
        served.add(current)
    return {"provider": row.get("provider"), "model": current,
            "roles": list(_ORCHESTRATOR_ROLES),
            "models": [{"id": m, "label": m.split("/")[-1]} for m in sorted(served)]}


@router.put("/api/chat/orchestrator-model", responses={400: {"description": "Bad request"}})
def orchestrator_model_set(body: _ChatModelBody) -> dict:
    """Set the model for the orchestrator's 2 agents (enhancer + planner)."""
    cur = _acfg.get("chat") if "chat" in _acfg.archetypes() else {}
    provider = body.provider or cur.get("provider") or "local"
    try:
        for role in _ORCHESTRATOR_ROLES:
            # Point at the CHAT slot's endpoint — the working multi-model
            # server. Reusing the role's own base_url would preserve a stale
            # per-model proxy (/proxy/<model>) and the picked model would 404.
            _acfg.set_role(role, provider, body.model,
                           base_url=cur.get("base_url"),
                           insecure_tls=bool(cur.get("insecure_tls")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "model": body.model, "roles": list(_ORCHESTRATOR_ROLES)}
