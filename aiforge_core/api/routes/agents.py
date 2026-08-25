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
from aiforge_core.config.env import ROLES

router = APIRouter()


# What each archetype does, for the Agents page.
_ROLE_DESCRIPTIONS = {
    "enhancer": "Cleans the raw request into a clear, unambiguous spec before planning.",
    "architect": "Designs the file/module structure and approach for the spec.",
    "triage": "Routes the work — trivial fast-path vs full pipeline.",
    "planner": "Splits the design into ordered, concrete subtasks.",
    "verifier": "Critiques the plan before code is written (merges the verify_* verdicts).",
    "researcher": "Gathers the codebase/external context the plan needs.",
    "doer": "Writes the actual code and runs the tools that implement each subtask.",
    "refiner": "Polishes the doer's output — cleanup, edge cases — inside the work loop.",
    "feedback": "In-loop reviewer: checks each pass and feeds corrections back.",
    "learner": "Persists durable lessons/memory so future runs start smarter.",
    "verify_correctness": "Axis critic: is the plan/code correct and complete?",
    "verify_scope": "Axis critic: does it stay within the requested scope?",
    "verify_risk": "Axis critic: flags risky, destructive, or fragile changes.",
    "ctx_memory": "Parallel gatherer: pulls relevant past decisions / memory.",
    "ctx_repomap": "Parallel gatherer: builds a map of the repo structure.",
    "ctx_conventions": "Parallel gatherer: extracts the project's coding conventions.",
    "gap_eval": "Research-completeness critic: drives the bounded re-search loop.",
    "live_verifier": "Boots + exercises the built project against a live-verify recipe.",
    "chat": "The dashboard chat assistant's own model slot (independent of the pipeline).",
}

_ORCHESTRATOR_ROLES = {"enhancer", "architect", "planner"}
_FANOUT_ROLES = {"ctx_memory", "ctx_repomap", "ctx_conventions",
                 "verify_correctness", "verify_scope", "verify_risk",
                 "gap_eval", "live_verifier"}


def _role_group(role: str) -> str:
    if role == "chat":
        return "chat"
    if role in _ORCHESTRATOR_ROLES:
        return "orchestrator"
    return "fanout" if role in _FANOUT_ROLES else "pipeline"


def _role_activity(_name: str) -> tuple:
    """``(last_activity_iso, lifetime_turns, active_tickets)``.

    SQLite-degraded: the per-role activity rollup was Postgres-only (used
    ``FILTER``), so on the embedded SQLite backend it returns nulls — the
    static role catalogue still renders, so the Agents / Home views work
    everywhere.
    """
    return (None, 0, [])


def _visible_roles() -> list[str]:
    """The REAL archetype list (config.agent_config) — not the 5 legacy env.py
    ROLES — so the page shows enhancer/architect/planner and every other
    configured agent. Only the synthetic default is hidden; every real agent
    (incl. the chat slot + the context/verifier fan-out sub-agents) is shown."""
    try:
        roles = _acfg.archetypes()
    except Exception:  # noqa: BLE001
        roles = list(ROLES.keys())
    return [r for r in roles if r != "_default"]


def _agent_row(name: str) -> dict:
    """One role's catalogue entry. Per-role model/provider come from
    agent_config; max_turns/tool_allowlist only exist for the legacy ROLES and
    default sensibly when absent."""
    rc = ROLES.get(name)
    try:
        cfg = _acfg.get(name)
    except Exception:  # noqa: BLE001
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    last_iso, turns, active = _role_activity(name)
    return {
        "role": name,
        "description": _ROLE_DESCRIPTIONS.get(name, ""),
        "group": _role_group(name),
        "model": cfg.get("model") or (rc.model if rc else ""),
        # "transport" doubles as the provider chip in the UI: legacy roles
        # report their transport; new orchestrator roles report the provider.
        "transport": (rc.transport if rc
                      else cfg.get("provider") or "openai_compatible"),
        "max_turns": rc.max_turns if rc else None,
        "tool_allowlist": list(rc.tool_allowlist) if rc else [],
        "last_activity": last_iso,
        "lifetime_turns": turns,
        "active_tickets": active,
    }


@router.get("/api/agents")
def list_agents() -> list[dict]:
    """Static role catalogue + dynamic last-activity from ticket_events."""
    return [_agent_row(name) for name in _visible_roles()]


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


@router.put("/api/config/agents/{role}", responses={400: {"description": "Bad request"}})
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


def _saved_role_credentials(role: str, base_url: str, api_key: str | None,
                            insecure: bool) -> tuple[str, str | None, bool]:
    """Fill blanks from the role's SAVED config (env + stored row).

    The UI never echoes the stored token back into the field, so without this
    fallback a Test issued right after Save would send no token and 401.
    """
    if not role or role not in _acfg.archetypes():
        return base_url, api_key, insecure
    try:
        rl = _acfg.resolve_litellm(role)
    except Exception:  # noqa: BLE001
        return base_url, api_key, insecure
    if not base_url:
        base_url = rl.get("api_base") or ""
    if not api_key:
        k = rl.get("api_key")
        api_key = None if (not k or k == "not-needed") else k
    return base_url, api_key, insecure or bool(rl.get("insecure_tls"))


@router.post("/api/providers/test")
def providers_test(body: _ProviderTestBody) -> dict:
    """Test-connection for the home page. Probes ``{base_url}/models`` and
    returns ``{ok, models[]}`` (or ``{ok:false, error}``).

    Blank ``base_url`` / ``api_key`` fall back to the saved config for
    ``role`` (resolved via env + stored row), so Test works right after
    Save.
    """
    from aiforge_core.llm.providers.openai_compatible import probe
    base_url, api_key, insecure = _saved_role_credentials(
        body.role, (body.base_url or "").strip(),
        (body.api_key or "").strip() or None, bool(body.insecure_tls))
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
        from aiforge_core.config import agent_config, model_registry
        model_registry.auto_assign(agent_config.archetypes())
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/agents/models", status_code=201, responses={400: {"description": "Bad request"}})
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
    # Vision auto-detect for EVERY model added with vision=='auto': probe the
    # endpoint with a test image and persist yes/no (heuristic fallback when the
    # probe is inconclusive) — in the BACKGROUND so the request returns now.
    # Opt out: AIFORGE_VISION_PROBE_ON_ADD=0.
    try:
        if ((body.vision or "auto") == "auto"
                and os.environ.get("AIFORGE_VISION_PROBE_ON_ADD", "1")
                not in ("0", "false")):
            import threading

            from aiforge_core.runtime import vision_detect
            threading.Thread(
                target=vision_detect.classify_and_store_vision,
                args=(row.get("id"), body.model, body.base_url, body.api_key),
                daemon=True, name="vision-probe-add").start()
    except Exception:  # noqa: BLE001 — probe must never break model-add
        pass
    return row


@router.put("/api/agents/models/{model_id}", responses={404: {"description": "Not found"}})
def models_update(model_id: str, body: _ModelBody) -> dict:
    from aiforge_core.config import model_registry
    row = model_registry.update_model(
        model_id, label=body.label, model=body.model, base_url=body.base_url,
        api_key=body.api_key, insecure_tls=body.insecure_tls, vision=body.vision,
        thinking=body.thinking, context_window=body.context_window)
    if row is None:
        raise HTTPException(404, f"model {model_id} not found")
    # A model change invalidates the per-model capability probe caches.
    try:
        from aiforge_core.runtime import chat_media
        from aiforge_core.runtime.chat_agent import _native
        chat_media.reset_vision_cache()
        _native.reset_native_cache()
    except Exception:  # noqa: BLE001
        pass
    return row


@router.delete("/api/agents/models/{model_id}", status_code=204, responses={404: {"description": "Not found"}})
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


@router.post("/api/agents/models/{model_id}/apply", responses={404: {"description": "Not found"}})
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
    from aiforge_core.config import agent_config, model_registry
    return {"assignments": model_registry.suggest_assignments(agent_config.archetypes())}


@router.post("/api/agents/auto-assign")
def agents_auto_assign(body: _AutoAssignBody) -> dict:
    """Auto-choose the best model for every agent BY CAPABILITY and apply it.
    Thinking/reasoning roles → a reasoning model, code roles → a fast coder,
    vision-needing → a vision model (larger context wins within a tier)."""
    from aiforge_core.config import agent_config, model_registry
    roles = body.roles or agent_config.archetypes()
    if body.dry_run:
        return {"assignments": model_registry.suggest_assignments(roles), "applied": False}
    out = model_registry.auto_assign(roles)
    out["applied"] = True
    return out


@router.put("/api/agents/v2/{role}/config", responses={400: {"description": "Bad request"}, 404: {"description": "Not found"}})
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


@router.put("/api/agents/v2/profile/{name}", responses={404: {"description": "Not found"}})
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
