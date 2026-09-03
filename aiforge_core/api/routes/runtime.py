"""Runtime + metrics routes (/api/runtime/*, /api/metrics) — split out of api.py.

Operator runtime toggles (rate limits, LLM backend selection, force-full-
pipeline, per-role session params, global LLM token settings), the perf +
cost dashboards, and the on-demand operational metrics rollup. Handlers keep
their inline function-local imports; env writes go through the shared
``_persist_env`` (runtime.env) so they survive a restart.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from aiforge_core.api._shared import _persist_env
from aiforge_core.api._shared import env_truthy as _env_truthy

router = APIRouter()


@router.get("/api/runtime/token_usage")
def token_usage(ticket: str | None = None) -> dict:
    """Token totals per role per ticket — empty under new aiforge_agents
    pipeline (the legacy GA tokens module was removed). Token tracking
    will be re-added on the new orchestrator's audit path.
    """
    return {"all": {}, "per_ticket": {}}


@router.get("/api/runtime/rate_limits")
def get_rate_limits() -> dict:
    """Active rate-limit config + bucket state per provider.

    UI uses this to render bucket gauges and the limit-edit form.
    """
    from aiforge_core.llm import list_providers as _list
    from aiforge_core.llm import providers as _providers
    from aiforge_core.llm import rl_state as _state
    out: list[dict] = []
    for entry in _list():
        name = entry["name"]
        prov = _providers.get(name)
        declared = prov.rate_limits() if prov is not None else None
        rpm_env = os.environ.get(f"AIFORGE_{name.upper()}_RPM")
        tpm_env = os.environ.get(f"AIFORGE_{name.upper()}_TPM")
        rec = {
            "provider": name,
            "available": entry["available"],
            "declared": declared,
            "effective_rpm": float(rpm_env) if rpm_env else (declared or {}).get("rpm", 0),
            "effective_tpm": float(tpm_env) if tpm_env else (declared or {}).get("tpm", 0),
            "env_override_rpm": rpm_env,
            "env_override_tpm": tpm_env,
            "state": _state(name),
        }
        out.append(rec)
    return {"providers": out, "max_wait_s": int(os.environ.get("AIFORGE_LLM_MAX_WAIT_S", 120))}


@router.put("/api/runtime/rate_limits", responses={400: {"description": "Bad request"}})
def set_rate_limit(payload: dict) -> dict:
    """Tighten/loosen a provider's RPM or TPM at runtime.

    payload: ``{"provider": "openai_compatible", "rpm": 30, "tpm": 500000}``.
    Either field optional; sets ``AIFORGE_<PROVIDER>_RPM/_TPM`` env
    + persists to runtime.env.
    """
    provider = (payload.get("provider") or "").strip().lower()
    if not provider:
        raise HTTPException(400, "provider required")
    written: dict = {}
    for key in ("rpm", "tpm"):
        v = payload.get(key)
        if v is None:
            continue
        env_name = f"AIFORGE_{provider.upper()}_{key.upper()}"
        os.environ[env_name] = str(v)
        _persist_env(env_name, str(v))
        written[key] = v
    return {"provider": provider, "set": written}


@router.get("/api/runtime/llm_backend")
def get_llm_backend() -> dict:
    """Active LLM backend for all agents + the provider registry."""
    from aiforge_core.llm import list_providers as _list
    providers = _list()
    avail_names = [p["name"] for p in providers if p["available"]]
    value = (
        os.environ.get("AIFORGE_PRIMARY_BACKEND")
        or os.environ.get("AIFORGE_DOER_PRIMARY_BACKEND")
        or "local"
    ).lower()
    if value not in avail_names:
        value = "local"
    return {
        "backend": value,
        "options": avail_names,
        "providers": providers,
        # Legacy field for old UI builds. The bundled gemini provider was
        # removed (a cloud endpoint that self-activated from an env var), so
        # this is now always False. Kept rather than deleted because an old
        # cached bundle reading `undefined` here renders differently than
        # reading `false`.
        "gemini_available": False,
    }


@router.put("/api/runtime/llm_backend", responses={400: {"description": "Bad request"}})
def set_llm_backend(payload: dict) -> dict:
    """Flip the active LLM backend for every agent.

    Affects runs started AFTER this call. graph-runner picks up the
    new value next poll-cycle restart (~10-15s).
    """
    from aiforge_core.llm import list_providers as _list
    avail = {p["name"] for p in _list() if p["available"]}
    backend = (payload.get("backend") or "").strip().lower()
    if backend not in avail:
        raise HTTPException(
            400, f"backend must be one of {sorted(avail)}; got {backend!r}"
        )
    os.environ["AIFORGE_PRIMARY_BACKEND"] = backend
    _persist_env("AIFORGE_PRIMARY_BACKEND", backend)
    # Drop the legacy doer-only key so it doesn't shadow the global flag.
    os.environ.pop("AIFORGE_DOER_PRIMARY_BACKEND", None)
    return {"backend": backend, "persisted": True}


# Legacy-compat aliases — keep older callers working until UI ships.
@router.get("/api/runtime/doer_backend")
def get_doer_backend_alias() -> dict:
    return get_llm_backend()


@router.put("/api/runtime/doer_backend")
def set_doer_backend_alias(payload: dict) -> dict:
    return set_llm_backend(payload)


@router.get("/api/runtime/force_full_pipeline")
def get_force_full_pipeline() -> dict:
    """Whether the triage fast-path is disabled (every agent always runs)."""
    return {"enabled": _env_truthy("AIFORGE_FORCE_FULL_PIPELINE")}


@router.put("/api/runtime/force_full_pipeline")
def set_force_full_pipeline(payload: dict) -> dict:
    """Toggle running the FULL pipeline (skip the triage 'trivial' fast-path).
    Affects runs started after this call."""
    enabled = bool(payload.get("enabled"))
    val = "1" if enabled else "0"
    os.environ["AIFORGE_FORCE_FULL_PIPELINE"] = val
    try:
        _persist_env("AIFORGE_FORCE_FULL_PIPELINE", val)
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": enabled, "persisted": True}


@router.get("/api/runtime/compaction")
def get_compaction() -> dict:
    """Whether memory compaction's LLM folds are turned off.

    ENABLED BY DEFAULT (unset ⇒ compaction ON): the rate limiter caps it at
    compaction_rpm (default 5/min). One source of truth for the flag —
    ``compact_window.disabled()`` — shared by the daily pass, the boot fold and
    the sync-loop OKF fold. Only an explicit ``1``/``true``/``yes`` disables."""
    from aiforge_core.runtime import compact_window as _cw
    return {"disabled": _cw.disabled()}


@router.put("/api/runtime/compaction")
def set_compaction(payload: dict) -> dict:
    """Enable/disable ALL memory compaction LLM folds (the daily pass, the boot
    fold and the sync-loop OKF fold — one switch). ENABLED by default. Takes
    effect on the next process boot; the running scheduler keeps its current
    registration until then, and the sync-loop fold picks it up live."""
    disabled = bool(payload.get("disabled"))
    val = "1" if disabled else "0"
    os.environ["AIFORGE_COMPACT_DISABLE"] = val
    try:
        _persist_env("AIFORGE_COMPACT_DISABLE", val)
    except Exception:  # noqa: BLE001
        pass
    return {"disabled": disabled, "persisted": True}


@router.post("/api/runtime/session_param", responses={400: {"description": "Bad request"}})
def session_param(payload: dict) -> dict:
    """Per-role LLM param tuning at runtime (GA /session.key=value, commit
    127a4e6). Updates the agent_config so the NEXT agent run picks new
    values. Doesn't affect a currently-running agent.

    payload: ``{"role": "doer|planner|...", "key": "temperature|max_tokens|...", "value": "..."}``
    """
    role = (payload.get("role") or "").strip()
    key = (payload.get("key") or "").strip()
    value = payload.get("value")
    if not role or not key or value is None:
        raise HTTPException(400, "role, key, value required")
    env_var = f"AIFORGE_{role.upper()}_{key.upper()}"
    os.environ[env_var] = str(value)
    return {"set": env_var, "value": str(value)}


# ─────────────────────────── Metrics ────────────────────────────────────
@router.get("/api/metrics")
def metrics() -> dict:
    """Operational metrics.

    These aggregates were computed over the Postgres ``tickets`` /
    ``ticket_events`` / ``memories`` tables, which were removed with the
    Postgres backend (SQLite-only build). Returns the empty-shaped result so
    any caller keeps working; the SQLite ticket/memory stores expose their own
    counts via their dedicated endpoints.
    """
    return {
        "ticket_grid": [],
        "feedback_verdicts": {"pass": 0, "fail": 0, "implicit_pass": 0},
        "stop_reasons": [],
        "reclaim_distribution": [],
        "memory_by_tier": [],
        "top_facts_by_hits": [],
        "activity_24h": [],
    }


# Global LLM token knobs — operator-chosen, no hardcoded constant wins over
# an explicit value. max_output_tokens = generation cap (file-write budget);
# context_window = assumed input window (escalation sizing).
class _RuntimeSettingsBody(BaseModel):
    max_output_tokens: int | None = Field(None, ge=256, le=1_000_000)
    context_window: int | None = Field(None, ge=1024, le=10_000_000)
    # 0/1 — force-treat the chat model as vision-capable (auto-detect still
    # applies when 0). Lets the user enable image Q&A for a self-hosted
    # multimodal model the allowlist doesn't recognise.
    vision_capable: int | None = Field(None, ge=0, le=1)
    # 0/1 — cave mode: send the agents the leanest useful context.
    cave_mode: int | None = Field(None, ge=0, le=1)
    # 0/1 — LLM-written, code-aware compaction (else cheap heuristic breadcrumb).
    compact_llm: int | None = Field(None, ge=0, le=1)
    # 0/1 disable flags for each dynamic-context block (default 0 = injected).
    ctx_no_recall: int | None = Field(None, ge=0, le=1)
    ctx_no_mentions: int | None = Field(None, ge=0, le=1)
    ctx_no_skills: int | None = Field(None, ge=0, le=1)
    ctx_no_workflows: int | None = Field(None, ge=0, le=1)
    ctx_no_repomap: int | None = Field(None, ge=0, le=1)
    ctx_no_summary: int | None = Field(None, ge=0, le=1)
    # Per-turn chat budget guards. Runaway guards, not task budgets — a turn
    # still making progress extends them chat_cap_extensions times.
    # ge=0 — 0 means NO step cap, the same convention the deadline below uses.
    # This validator is the ONLY write path for the setting, so a floor of 1
    # here made the store's own lower bound irrelevant: the UI 422'd.
    chat_safety_cap: int | None = Field(None, ge=0, le=1_000_000)
    chat_turn_deadline_s: int | None = Field(None, ge=0, le=86_400)
    chat_cap_extensions: int | None = Field(None, ge=0, le=50)
    # ge=1: a background run has no Stop button, so it is never uncapped.
    chat_unattended_cap: int | None = Field(None, ge=1, le=1_000_000)
    # 0 = no ceiling. This is the machine-wide GLOBAL cap; the two sub-ceilings
    # below carve it up per category.
    llm_max_rpm: int | None = Field(None, ge=0, le=100_000)
    # Per-category sub-ceilings. compaction = memory/learner folding (small so it
    # never starves chat); chat = everything else. 0 = only the global applies.
    compaction_rpm: int | None = Field(None, ge=0, le=100_000)
    chat_rpm: int | None = Field(None, ge=0, le=100_000)
    # Rate-limit response knobs. Bounds MUST match _BOUNDS in runtime_settings:
    # this validator is the only write path, so a mismatch here makes the
    # store's own bound unreachable and the UI 422s on a value it offers.
    llm_rate_limit_backoff_s: int | None = Field(None, ge=0, le=3_600)
    llm_rate_limit_cap_s: int | None = Field(None, ge=1, le=3_600)
    # Names to FORGET, so those knobs fall back to env / built-in default
    # (the store otherwise shadows the documented env var forever).
    unset: list[str] | None = None


@router.get("/api/runtime/llm-settings")
def llm_settings_get() -> dict:
    from aiforge_core.config import runtime_settings as _rs
    return _rs.all_settings()


@router.put("/api/runtime/llm-settings", responses={400: {"description": "Bad request"}})
def llm_settings_set(body: _RuntimeSettingsBody) -> dict:
    from aiforge_core.config import runtime_settings as _rs

    data = body.model_dump()
    drop = data.pop("unset", None) or []
    vals = {k: v for k, v in data.items() if v is not None}
    if not vals and not drop:
        raise HTTPException(400, "no settings provided")
    both = sorted(set(drop) & set(vals))
    if both:
        # Writing a value and forgetting it in the same request is a
        # contradiction; answering 200 to it hides which half won.
        raise HTTPException(400, "cannot set and unset the same knob: "
                                 + ", ".join(both))
    unknown = sorted(n for n in drop if n not in _rs._SPEC)
    if unknown:
        raise HTTPException(400, "unknown setting(s): " + ", ".join(unknown))
    try:
        out = _rs.set_many(vals) if vals else _rs.all_settings()
        return _rs.unset(drop) if drop else out
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/runtime/perf")
def runtime_perf(reset: bool = False) -> dict:
    """Per-step perf snapshot, backed by the ndjson perf recorder.

    Samples are appended by ``aiforge_core.runtime.perf_recorder`` at the LLM
    call boundary and at each chat/doer tool dispatch. ``reset`` truncates the
    recorder's ndjson and returns an empty snapshot."""
    from aiforge_core.runtime import perf_recorder
    if reset:
        perf_recorder.reset()
        return {"rows": [], "reset": True}
    return {"rows": perf_recorder.aggregate(), "reset": False}


@router.get("/api/runtime/cost")
def runtime_cost(
    ticket: str | None = None,
    group_by: str | None = None,
    days_back: int = 30,
) -> dict:
    """USD totals.

    Without params: in-memory global + per-ticket map.
    ``?ticket=X`` returns single ticket counters.
    ``?group_by=day|role|model|ticket`` runs SQL rollup over
    ``llm_costs`` for the last ``days_back`` days.
    """
    from aiforge_core.observability import cost as _cost
    if group_by:
        return {"group_by": group_by, "days_back": days_back,
                "rows": _cost.rollup(group_by, days_back=days_back)}
    return _cost.snapshot(ticket)


class EgressHostsBody(BaseModel):
    extra_hosts: list[str] = Field(
        default_factory=list,
        description="Hosts to allow in ADDITION to the configured integrations. "
                    "A full URL or a bare host[:port] both work; the host is "
                    "what gets stored.")


@router.get("/api/runtime/egress_hosts")
def egress_hosts_get() -> dict:
    """What this box may talk to, and where each entry came from.

    Egress enforcement is always on and the list defaults to DENY, so the
    screen has to show the DERIVED entries too — otherwise an operator adds
    their Jira host by hand and is then surprised that deleting it changes
    nothing."""
    from aiforge_core.config import egress_hosts

    return egress_hosts.describe()


@router.put("/api/runtime/egress_hosts",
            responses={400: {"description": "Bad request"}})
def egress_hosts_put(body: EgressHostsBody) -> dict:
    """Replace the operator's extra hosts. Derived entries are NOT editable
    here — they follow the integration config, so the way to remove one is to
    unconfigure the integration rather than to prune a list that will silently
    regrow."""
    from aiforge_core.config import egress_hosts

    if len(body.extra_hosts) > 100:
        raise HTTPException(status_code=400,
                            detail="too many hosts (max 100)")
    for raw in body.extra_hosts:
        if len(str(raw)) > 253:      # max DNS name length
            raise HTTPException(status_code=400,
                                detail=f"host too long: {str(raw)[:40]}…")
    try:
        saved = egress_hosts.set_stored_hosts(body.extra_hosts)
    except ValueError as exc:
        # A rejected shape is the operator's typo, not a server fault — and the
        # message names what is wrong with which entry.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "extra_hosts": saved, **egress_hosts.describe()}
