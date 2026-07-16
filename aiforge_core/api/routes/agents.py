"""Agent + model-registry config routes — split out of api.py (APIRouter).

Per-archetype provider/model config (v1 + v2), the model registry, provider
connectivity test, capability-based auto-assign, and profile presets. Handlers
keep their inline function-local imports; the request models + the
_reassign_by_capability helper moved here VERBATIM.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from aiforge_core.config import agent_config as _acfg

router = APIRouter()


@router.get("/api/config/agents")
def config_agents_list() -> dict:
    """Per-archetype provider + model map. UI Settings calls this.

    Surfaces the 6 v5 archetype roles
    (architect/planner/verifier/doer/feedback/learner) — the live ADK
    SequentialAgent + external Architect.
    """
    full = _acfg.load_all()
    visible = {r: full[r] for r in _acfg._ARCHETYPES if r in full}
    return {
        "roles": visible,
        "archetype_order": list(_acfg._ARCHETYPES),
        "providers": {
            p["id"]: {"label": p["label"],
                      "default_model": p["default_model"]}
            for p in _acfg.list_providers()
        },
    }


class _AgentConfigBody(BaseModel):
    provider: str = Field(..., description="One of agent_config.PROVIDERS keys")
    model: str = Field(..., description="Model identifier for the provider")


@router.put("/api/config/agents/{role}")
def config_agents_set(role: str, body: _AgentConfigBody) -> dict:
    try:
        cfg = _acfg.set_role(role, body.provider, body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"role": role, **cfg}


class _AgentConfigV2Body(BaseModel):
    provider: str = Field(..., description="One of agent_config.PROVIDERS keys")
    model: str = Field(..., min_length=1,
                       description="Model identifier for the provider")
    base_url: str | None = Field(
        None, description="Optional override; null = provider default")
    api_key: str | None = Field(
        None, description="Optional API key (openai_compatible cloud-with-key); "
                          "blank = no token")
    insecure_tls: bool = Field(
        False, description="Skip TLS verification for this endpoint only "
                           "(self-signed / internal HTTPS box)")


class _ProviderTestBody(BaseModel):
    base_url: str | None = Field(
        None, description="OpenAI-compatible base URL to probe; falls back "
                          "to the saved base_url for `role` when omitted")
    api_key: str | None = Field(
        None, description="Bearer key; falls back to the saved token for "
                          "`role` when omitted (UI never echoes the secret)")
    insecure_tls: bool = Field(
        False, description="Skip TLS verification for this probe only")
    role: str | None = Field(
        None, description="Archetype whose saved creds fill blank fields, "
                          "so Test works after Save without re-typing the token")


@router.get("/api/agents/v2/config")
def agents_v2_config() -> dict:
    """Return ``{role: {provider, model, base_url|null}}`` for the 6
    v5 archetypes (architect/planner/verifier/doer/feedback/learner)."""
    full = _acfg.load_all()
    out: dict[str, dict[str, Any]] = {}
    for role in _acfg.archetypes():
        row = full.get(role) or {}
        out[role] = {
            "provider": row.get("provider"),
            "model": row.get("model"),
            "base_url": row.get("base_url"),
            # Never echo the secret — just whether one is stored.
            "api_key_set": bool(row.get("api_key")),
            "insecure_tls": bool(row.get("insecure_tls")),
        }
    return out


@router.post("/api/providers/test")
def providers_test(body: _ProviderTestBody) -> dict:
    """Test-connection for the home page. Probes ``{base_url}/models`` and
    returns ``{ok, models[]}`` (or ``{ok:false, error}``).

    Blank ``base_url`` / ``api_key`` fall back to the saved config for
    ``role`` (resolved via env + stored row), so Test works right after
    Save — the UI never echoes the stored token back into the field, so
    without this fallback a post-Save Test would send no token and 401.
    """
    from aiforge_core.llm.providers.openai_compatible import probe
    base_url = (body.base_url or "").strip()
    api_key = (body.api_key or "").strip() or None
    insecure = bool(body.insecure_tls)
    if body.role and body.role in _acfg.archetypes():
        try:
            rl = _acfg.resolve_litellm(body.role)
        except Exception:
            rl = {}
        if not base_url:
            base_url = rl.get("api_base") or ""
        if not api_key:
            k = rl.get("api_key")
            api_key = None if (not k or k == "not-needed") else k
        insecure = insecure or bool(rl.get("insecure_tls"))
    logging.getLogger("aiforge.api").info(
        "POST /api/providers/test role=%s base_url=%s insecure_tls=%s token=%s",
        body.role, base_url, insecure, "yes" if api_key else "no")
    return probe(base_url, api_key, insecure=insecure)


@router.get("/api/agents/v2/providers")
def agents_v2_providers() -> list[dict]:
    """Catalog payload for the Settings UI: each provider with its
    available models inline. Includes dynamic discovery for local
    (LM Studio /v1/models) and ollama_cloud (5-min cached)."""
    out: list[dict[str, Any]] = []
    for prov in _acfg.list_providers():
        try:
            models = _acfg.list_models(prov["id"])
        except Exception:
            models = []
        out.append({**prov, "models": models})
    return out


class _ModelBody(BaseModel):
    label: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    insecure_tls: bool | None = None
    vision: str | None = Field(None, description="'auto' | 'yes' | 'no'")
    thinking: str | None = Field(None, description="reasoning model: 'auto' | 'yes' | 'no'")
    context_window: int | None = Field(None, ge=0, le=10_000_000,
                                       description="per-model input window (tokens); 0 = use global")


class _ApplyModelBody(BaseModel):
    roles: list[str] = Field(..., description="agent roles to point at this model")


@router.get("/api/agents/models")
def models_list() -> dict:
    from aiforge_core.config import model_registry
    return {"models": model_registry.list_models()}


def _reassign_by_capability() -> None:
    """Re-run capability-based agent auto-assignment. Called whenever the model
    set changes so the system always chooses each agent's model internally — no
    manual picking. Best-effort; never breaks the mutation that triggered it."""
    if os.environ.get("AIFORGE_AUTO_ASSIGN_AGENTS", "1") in ("0", "false"):
        return
    try:
        from aiforge_core.config import model_registry, agent_config
        model_registry.auto_assign(agent_config.archetypes())
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/agents/models", status_code=201)
def models_add(body: _ModelBody) -> dict:
    from aiforge_core.config import model_registry
    if not (body.model or "").strip():
        raise HTTPException(400, "model id is required")
    try:
        row = model_registry.add_model(
            label=body.label or body.model, model=body.model,
            base_url=body.base_url or "", api_key=body.api_key,
            insecure_tls=(True if body.insecure_tls is None else bool(body.insecure_tls)),
            vision=body.vision or "auto", thinking=body.thinking or "auto",
            context_window=body.context_window or 0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _reassign_by_capability()          # auto-decide agents on model add
    return row


@router.put("/api/agents/models/{model_id}")
def models_update(model_id: str, body: _ModelBody) -> dict:
    from aiforge_core.config import model_registry
    row = model_registry.update_model(
        model_id, label=body.label, model=body.model, base_url=body.base_url,
        api_key=body.api_key, insecure_tls=body.insecure_tls, vision=body.vision,
        thinking=body.thinking, context_window=body.context_window)
    if row is None:
        raise HTTPException(404, f"model {model_id} not found")
    # A vision change invalidates the probe cache for that model.
    try:
        from aiforge_core.runtime import chat_media
        chat_media.reset_vision_cache()
    except Exception:  # noqa: BLE001
        pass
    return row


@router.delete("/api/agents/models/{model_id}", status_code=204)
def models_delete(model_id: str) -> None:
    from aiforge_core.config import model_registry
    if not model_registry.remove_model(model_id):
        raise HTTPException(404, f"model {model_id} not found")
    _reassign_by_capability()          # re-decide agents after a model is removed


@router.post("/api/agents/models/sync")
def models_sync() -> dict:
    """Populate the registry from the agents' current per-role config (so it's
    not empty when models are already wired)."""
    from aiforge_core.config import model_registry
    res = model_registry.sync_from_config()
    _reassign_by_capability()          # auto-decide agents after sync
    return res


@router.post("/api/agents/models/{model_id}/apply")
def models_apply(model_id: str, body: _ApplyModelBody) -> dict:
    from aiforge_core.config import model_registry
    try:
        return model_registry.apply_to_roles(model_id, body.roles)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


class _AutoAssignBody(BaseModel):
    roles: list[str] | None = Field(None, description="roles to assign; default = all archetypes")
    dry_run: bool = Field(False, description="compute the plan without applying it")


@router.get("/api/agents/auto-assign")
def agents_auto_assign_preview() -> dict:
    """Preview capability-based assignments (thinking→reasoning model, coder→fast
    coder, vision→vision model) for every archetype — no changes applied."""
    from aiforge_core.config import model_registry, agent_config
    return {"assignments": model_registry.suggest_assignments(agent_config.archetypes())}


@router.post("/api/agents/auto-assign")
def agents_auto_assign(body: _AutoAssignBody) -> dict:
    """Auto-choose the best model for every agent BY CAPABILITY and apply it.
    Thinking/reasoning roles → a reasoning model, code roles → a fast coder,
    vision-needing → a vision model (larger context wins within a tier)."""
    from aiforge_core.config import model_registry, agent_config
    roles = body.roles or agent_config.archetypes()
    if body.dry_run:
        return {"assignments": model_registry.suggest_assignments(roles), "applied": False}
    out = model_registry.auto_assign(roles)
    out["applied"] = True
    return out


@router.put("/api/agents/v2/{role}/config")
def agents_v2_set(role: str, body: _AgentConfigV2Body) -> dict:
    # "_default" is the global fallback every pipeline role inherits (the
    # home page's "Apply to all" writes it). Allowed alongside the named
    # archetypes so a single setting covers the ~16 internal roles.
    if role != _acfg._DEFAULT_KEY and role not in _acfg.archetypes():
        raise HTTPException(404, f"unknown archetype: {role}")
    if body.provider not in _acfg.PROVIDERS:
        raise HTTPException(400, f"unknown provider: {body.provider}")
    if not body.model or not body.model.strip():
        raise HTTPException(400, "model cannot be empty")
    base_url = body.base_url.strip() if body.base_url else None
    api_key = body.api_key.strip() if body.api_key else None
    try:
        cfg = _acfg.set_role(role, body.provider, body.model,
                             base_url=base_url or None,
                             api_key=api_key or None,
                             insecure_tls=body.insecure_tls)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "role": role,
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "api_key_set": bool(cfg.get("api_key")),
        "insecure_tls": bool(cfg.get("insecure_tls")),
    }


@router.get("/api/agents/v2/profiles")
def agents_v2_profiles_list() -> dict:
    """Bundled profile presets — apply one to assign all 9 archetypes
    to the same provider/model in one call."""
    return {
        "profiles": [
            {"name": name, **spec}
            for name, spec in _acfg.PROFILES.items()
        ]
    }


@router.put("/api/agents/v2/profile/{name}")
def agents_v2_profile_apply(name: str) -> dict:
    """Bulk-apply a profile preset to every archetype.

    Returns the resulting per-role map. After applying, individual
    archetypes can still be flipped via PUT /api/agents/v2/{role}/config.
    """
    try:
        out = _acfg.apply_profile(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"profile": name, "roles": out}


@router.post("/api/agents/v2/reset")
def agents_v2_reset(keep_default: bool = Query(False)) -> dict:
    """Wipe the saved per-role agent config for a clean reconfigure.

    Removes stale per-role rows that can shadow a newly-set global default.
    ``keep_default=true`` preserves the global ``_default`` row and clears only
    the per-role overrides."""
    return _acfg.reset(keep_default=keep_default)
