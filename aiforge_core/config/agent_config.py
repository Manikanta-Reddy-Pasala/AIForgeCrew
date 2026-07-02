"""Per-archetype model + provider config, persisted to a JSON file.

The 6 archetypes match the v5 production pipeline (see
``aiforge_core/agents/agents.yaml`` + ``runtime.adk_runner``):

    architect, planner, verifier, doer, feedback, learner

Architect is external (human-driven operator session) but still
configurable here for trace symmetry — its model pin is read by the
operator's external client. The other five run inside the ADK SequentialAgent:
``Planner → Verifier → LoopAgent[Doer, Feedback] → Learner``.

Each archetype can be flipped between providers (local mlx_lm / Ollama
Cloud / any OpenAI-compatible endpoint) without a redeploy. Env vars still
override at read time so ops keeps a final-say escape hatch.

Storage: ``$AIFORGE_CONFIG_DIR/agent_config.json`` (default ``~/.aiforge``).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# v6 production pipeline — what the live ADK SequentialAgent runs.
# Order matches execution: architect (external) → triage → planner →
# verifier → researcher → LoopAgent[doer, refiner, feedback] → learner.
_ARCHETYPES: tuple[str, ...] = (
    "architect", "planner", "verifier", "doer", "feedback", "learner",
    # Extended pipeline (2026-05-07).
    "triage", "researcher", "refiner",
    # Post-validator live boot (2026-05-23): runs the recipe under
    # aiforge_core/recipes/live_verify/<project>.md.
    "live_verifier",
    # Parallel context-gathering fan-out (ParallelAgent) — see
    # runtime.parallel_stages. Read-only gatherers, local-tier by default.
    "ctx_memory", "ctx_repomap", "ctx_conventions",
    # Parallel verifier fan-out (ParallelAgent) — three axis critics
    # merged into verifier_verdict.
    "verify_correctness", "verify_scope", "verify_risk",
    # Research-completeness critic (2026-06-18) — drives the bounded
    # research-gap re-search loop. Local-tier, tool-less.
    "gap_eval",
    # Conversational chat agent (2026-06-21) — the UI chat's own model
    # slot, configured on the home page; independent of the pipeline.
    "chat",
    # Parallel orchestrator layer-1 agents (2026-06-26): enhancer (analyze →
    # spec) and architect (file/structure plan) → planner splits. Configurable
    # so the splitter can run on a stronger reasoning model than the workers.
    "enhancer",
)
_ROLES = _ARCHETYPES

# Local default — resolved dynamically, in order:
#   1. AIFORGE_LOCAL_DEFAULT_MODEL env (operator pin)
#   2. first model id served by the local /v1/models endpoint (5-min cache)
#   3. neutral placeholder (only when the server is unreachable AND no env
#      pin AND nothing configured) — a generic id, NOT a hardcoded absolute
#      model path from one operator's laptop (that phantom path was confusing
#      on every other host and produced "Connection error" against a model
#      nobody configured).
_LOCAL_FALLBACK_MODEL = "local-model-unconfigured"
_LOCAL_DEFAULT_CACHE: list[Any] = [0.0, None]  # [ts, model_id]
_LOCAL_DEFAULT_TTL_S = 300.0
_FALLBACK_WARNED = [False]


def _local_default_model() -> str:
    """Resolve a fallback model id when none is configured.

    ``openai_compatible`` is the only provider now; its model list comes
    from the per-role ``/v1/models`` probe (UI-driven), so there is no
    discovery here. Returns the operator's env pin, else a neutral
    placeholder (with a one-time warning) when nothing is configured.
    """
    env = os.environ.get("AIFORGE_LOCAL_DEFAULT_MODEL")
    if env and env.strip():
        return env.strip()
    # Nothing configured and env unset. Don't fabricate a real-looking model
    # — return a neutral placeholder and tell the operator once how to fix it.
    if not _FALLBACK_WARNED[0]:
        _FALLBACK_WARNED[0] = True
        logging.getLogger("aiforge.agent_config").warning(
            "no model configured — using placeholder '%s' (every call will "
            "fail). Fix: set a model in the UI (Home), pin "
            "AIFORGE_LOCAL_DEFAULT_MODEL, or point a role's base_url at a live "
            "OpenAI-compatible endpoint.",
            _LOCAL_FALLBACK_MODEL)
    return _LOCAL_FALLBACK_MODEL

PROVIDERS: dict[str, dict[str, Any]] = {
    # Generic OpenAI-compatible endpoint — the only provider. User supplies
    # base_url (+ optional api_key) per role via the home page. Covers
    # OSS-no-token, LM Studio, OpenRouter, Groq, Together, vLLM, and
    # cloud-with-key. Blank key = no token.
    "openai_compatible": {
        "label": "OpenAI-compatible (any base URL — OSS / LM Studio / OpenRouter / cloud)",
        "litellm_prefix": "openai",
        "default_model": None,          # free-text / discovered via /v1/models
        "api_key_env": "AIFORGE_OPENAI_COMPAT_API_KEY",
        "api_key_default": "not-needed",  # OSS endpoints ignore the key
        "base_url": None,               # required per-role (UI-set)
    },
}

_DEFAULT_KEY = "_default"

# LiteLLM provider prefixes we leave untouched on a model id (already
# namespaced). One shared copy — resolve_litellm + the two cloud helpers
# all use it. ``anthropic/`` dropped with the provider purge.
KNOWN_PREFIXES = (
    "openai/", "azure/", "ollama/", "huggingface/",
    "mistral/", "groq/", "cohere/", "bedrock/",
)


def _global_default_row() -> dict[str, Any] | None:
    """The operator's one-endpoint default for EVERY role.

    The pipeline has ~16 roles (the 6 archetypes plus triage / researcher /
    refiner / ctx_* / verify_* / gap_eval / chat). Configuring each by hand
    is a footgun — an unconfigured role silently falls back to ``local`` and
    breaks the whole team flow. A single ``_default`` entry (written by the
    home page's "Apply to all", or via ``AIFORGE_DEFAULT_*`` env) is
    inherited by any role without an explicit per-role override.

    Priority: persisted ``_default`` row > ``AIFORGE_DEFAULT_*`` env > none.
    """
    p = _path()
    if p.exists():
        try:
            disk = _fc.read_json(p) or {}
            d = disk.get(_DEFAULT_KEY)
            if isinstance(d, dict) and d.get("provider"):
                return d
        except Exception:  # noqa: BLE001
            pass
    prov = os.environ.get("AIFORGE_DEFAULT_PROVIDER")
    if prov:
        return {
            "provider": prov,
            "model": os.environ.get("AIFORGE_DEFAULT_MODEL", ""),
            "base_url": os.environ.get("AIFORGE_DEFAULT_BASE_URL"),
            "api_key": os.environ.get("AIFORGE_DEFAULT_API_KEY"),
            "insecure_tls": os.environ.get(
                "AIFORGE_DEFAULT_INSECURE_TLS", "").strip().lower()
                in ("1", "true", "yes", "on"),
        }
    return None


def _defaults() -> dict[str, dict[str, Any]]:
    """Per-role defaults. When a global ``_default`` is set, EVERY role
    inherits it (provider/model/base_url/api_key/insecure_tls); otherwise
    fall back to ``local`` with the dynamically-resolved local model id."""
    gd = _global_default_row()
    if gd and gd.get("provider"):
        model = (gd.get("model") or "").strip() or _local_default_model()
        return {
            role: {
                "provider": gd["provider"],
                "model": model,
                "base_url": gd.get("base_url"),
                "api_key": gd.get("api_key"),
                "insecure_tls": bool(gd.get("insecure_tls")),
            }
            for role in _ROLES
        }
    model = _local_default_model()
    return {
        role: {"provider": "openai_compatible", "model": model,
               "base_url": None}
        for role in _ROLES
    }


# ────────────────────────── Model catalog ──────────────────────────────
# openai_compatible is fully dynamic — the model list depends on the
# per-role base_url, so the UI fetches it on demand via the provider-probe
# endpoint (/v1/models) rather than from this static catalog.

MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "openai_compatible": [],
}

# Module-level cache retained for the reset() cache-clear contract (and
# tests) — no longer populated now that catalog discovery is UI-driven.
_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CATALOG_TTL_S = 300.0
_CATALOG_LOCK = threading.Lock()


def _enriched_catalog(provider: str) -> list[dict[str, Any]]:
    """Static curated list for a provider.

    ``openai_compatible`` carries no static catalog — its model list is
    discovered per-role from the configured endpoint's ``/v1/models`` via
    the UI provider-probe, not from here.
    """
    return list(MODEL_CATALOG.get(provider) or [])


from aiforge_core.config import _filecache as _fc


def _path() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "agent_config.json"


def load_all() -> dict[str, dict[str, Any]]:
    """Read the full per-role map, merging defaults for missing keys."""
    p = _path()
    cfg: dict[str, dict[str, Any]] = {k: dict(v)
                                      for k, v in _defaults().items()}
    gd = _global_default_row()
    if p.exists():
        try:
            disk = _fc.read_json(p)
            if isinstance(disk, dict):
                for role, row in disk.items():
                    if role in _ROLES and isinstance(row, dict):
                        # A configured NON-LOCAL global default (cloud /
                        # internal endpoint) must win over a STALE "bare local"
                        # per-role row — provider=local with no base_url and no
                        # api_key, i.e. a leftover from an old profile-apply or
                        # auto-discovery. Without this, you set your endpoint
                        # (e.g. https://chat.ai.internal/...) but triage keeps
                        # hitting 127.0.0.1:1234 on the old default model. Keep
                        # the seed (= the global default) for such rows.
                        if gd and gd.get("provider") and gd["provider"] != "local":
                            row_prov = row.get("provider") or "local"
                            if (row_prov == "local"
                                    and not row.get("base_url")
                                    and not row.get("api_key")):
                                continue   # cfg[role] already = global default
                        # ``cfg[role]`` is the global-default seed (or local
                        # fallback). A per-role row overlays it — but a row
                        # that OMITS base_url / api_key / insecure_tls must
                        # INHERIT them from the seed rather than null them.
                        # Without this, applying a profile (or a per-role
                        # Save) writes rows with base_url=None, which then
                        # shadow the operator's global endpoint and silently
                        # send every role back to http://127.0.0.1:1234 —
                        # the "I set one URL but it probes localhost" bug.
                        # An explicit per-role base_url still wins (lets us
                        # run mlx-lm on per-role ports). Inheritance of the
                        # endpoint only applies when the provider matches the
                        # seed (don't paste a cloud URL onto a local row).
                        seed = cfg[role]
                        provider = row.get("provider") or seed["provider"]
                        same_provider = provider == seed["provider"]
                        row_base = row.get("base_url")
                        row_key = row.get("api_key")
                        # Only inherit the seed's key when the row points at the
                        # SAME host (a different base_url is a different trust
                        # domain — don't leak the global cloud token to it). Since
                        # openai_compatible is the only provider, same_provider is
                        # always True, so the host check is what actually gates it.
                        # Compare HOSTNAMES (not the raw URL) so a trailing slash /
                        # case / explicit-port difference for the same endpoint
                        # doesn't wrongly drop the inherited key.
                        def _host(u: "str | None") -> "str | None":
                            try:
                                import urllib.parse as _up
                                return (_up.urlsplit(u or "").hostname or "").lower()
                            except Exception:  # noqa: BLE001
                                return None
                        _same_host = (not row_base) or (
                            _host(row_base) == _host(seed.get("base_url")))
                        cfg[role] = {
                            "provider": provider,
                            "model": row.get("model") or seed["model"],
                            "base_url": row_base or (
                                seed.get("base_url") if same_provider else None),
                            "api_key": row_key or (
                                seed.get("api_key") if (same_provider and _same_host) else None),
                            # Respect an EXPLICIT per-role insecure_tls (incl.
                            # a deliberate ``false`` to keep strict TLS) — only
                            # inherit the seed's flag when the row omits it.
                            "insecure_tls": (
                                bool(row["insecure_tls"])
                                if row.get("insecure_tls") is not None
                                else (same_provider
                                      and bool(seed.get("insecure_tls")))),
                        }
        except Exception as exc:  # noqa: BLE001
            # Corrupt / truncated agent_config.json → fall back to defaults,
            # but say so once (silent fallback made "my config vanished"
            # impossible to diagnose).
            logging.getLogger("aiforge.agent_config").warning(
                "agent_config.json unreadable (%s) — using defaults; "
                "fix or reset the file (run.sh --reset-config).", exc)
    # Env override: AIFORGE_<ROLE>_MODEL / AIFORGE_<ROLE>_PROVIDER /
    # AIFORGE_<ROLE>_BASE_URL / AIFORGE_<ROLE>_API_KEY. Always wins over
    # persisted JSON — ops escape hatch.
    for role in _ROLES:
        cfg[role].setdefault("api_key", None)
        env_model = os.environ.get(f"AIFORGE_{role.upper()}_MODEL")
        env_prov = os.environ.get(f"AIFORGE_{role.upper()}_PROVIDER")
        env_base = os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
        env_key = os.environ.get(f"AIFORGE_{role.upper()}_API_KEY")
        if env_model:
            cfg[role]["model"] = env_model
        if env_prov:
            cfg[role]["provider"] = env_prov
        if env_base:
            cfg[role]["base_url"] = env_base
        if env_key:
            cfg[role]["api_key"] = env_key
    return cfg


def get(role: str) -> dict[str, Any]:
    """Return resolved config for one role: ``{provider, model, base_url}``."""
    if role == _DEFAULT_KEY:
        return _global_default_row() or {}
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    return load_all()[role]


def _row_for(role: str) -> dict[str, Any]:
    """Like :func:`get`, but unknown roles (the ``enhancer`` /
    ``validator`` stages, not in the configurable archetype list) resolve
    to the global ``_default`` instead of raising — so they run on the
    operator's configured model. ``get`` stays strict for callers (e.g.
    observability) that depend on the raise.
    """
    if role in _ROLES:
        return get(role)
    gd = _global_default_row()
    if gd and gd.get("provider"):
        model = (gd.get("model") or "").strip() or _local_default_model()
        return {"provider": gd["provider"], "model": model,
                "base_url": gd.get("base_url"), "api_key": gd.get("api_key"),
                "insecure_tls": bool(gd.get("insecure_tls"))}
    return {"provider": "openai_compatible", "model": _local_default_model(),
            "base_url": None, "api_key": None, "insecure_tls": False}


# Cheap-tier roles — throwaway ops (triage, enhancer, titling) that should run
# on the smallest model, not contend with the big local model on a serial
# endpoint. Titling routes to 'triage' (see api.py), so this set covers it.
_CHEAP_ROLES = frozenset({"triage", "enhancer"})


def cheap_model_for(role: str) -> str | None:
    """Cheap-tier model fallback for a cheap role.

    Returns ``AIFORGE_CHEAP_MODEL`` when: the role is a cheap role, the env is
    set, AND there is NO explicit per-role pin (neither ``AIFORGE_<ROLE>_MODEL``
    env nor a persisted per-role row carrying a ``model``). Otherwise ``None`` —
    the caller keeps today's resolution. Unset ``AIFORGE_CHEAP_MODEL`` → always
    ``None`` (fully backward compatible)."""
    if role not in _CHEAP_ROLES:
        return None
    cheap = (os.environ.get("AIFORGE_CHEAP_MODEL") or "").strip()
    if not cheap:
        return None
    # An explicit per-role pin (env or persisted per-role row) wins.
    if (os.environ.get(f"AIFORGE_{role.upper()}_MODEL") or "").strip():
        return None
    try:
        p = _path()
        if p.exists():
            disk = _fc.read_json(p) or {}
            row = disk.get(role)
            if isinstance(row, dict) and (row.get("model") or "").strip():
                return None
    except Exception:  # noqa: BLE001
        pass
    return cheap


def set_role(role: str, provider: str, model: str,
             base_url: str | None = None,
             api_key: str | None = None,
             insecure_tls: bool = False) -> dict[str, Any]:
    """Persist {provider, model, base_url?, api_key?, insecure_tls?}.

    ``base_url`` is optional; when None, the provider's default is used at
    resolve time. ``api_key`` is optional too — used mainly by the
    ``openai_compatible`` provider for cloud-with-key endpoints; leave it
    blank for OSS-no-token. ``insecure_tls`` skips TLS verification for
    this endpoint only (self-signed / internal HTTPS box) — a per-role
    opt-out that avoids editing env files + restarting. Env vars still win
    on next read, which is desired for a one-off override without losing
    the saved default.
    """
    if role != _DEFAULT_KEY and role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not model or not model.strip():
        raise ValueError("model cannot be empty")
    if base_url is not None and not isinstance(base_url, str):
        raise ValueError("base_url must be string or None")
    if api_key is not None and not isinstance(api_key, str):
        raise ValueError("api_key must be string or None")
    with _LOCK:
        p = _path()
        disk: dict[str, dict[str, Any]] = {}
        if p.exists():
            try:
                disk = _fc.read_json(p) or {}
            except Exception:
                disk = {}
        row: dict[str, Any] = {
            "provider": provider, "model": model.strip(),
        }
        if base_url and base_url.strip():
            row["base_url"] = base_url.strip()
        else:
            row["base_url"] = None
        # Secret-preserving: the UI never echoes a stored api_key back, so
        # its field is blank on every reload. A blank key here therefore
        # means "leave the saved token untouched", NOT "wipe it" — else a
        # plain Save (or per-row Save after Apply-to-all) would silently
        # null the token. Pass api_key="" explicitly to clear (UI sends a
        # non-empty value only when the operator typed a new token).
        if api_key and api_key.strip():
            row["api_key"] = api_key.strip()
        else:
            row["api_key"] = (disk.get(role) or {}).get("api_key")
        row["insecure_tls"] = bool(insecure_tls)
        disk[role] = row
        _fc.write_json(p, disk)   # atomic + busts the read cache
    return get(role)


def reset(*, keep_default: bool = False) -> dict:
    """Wipe the persisted per-role config for a clean reconfigure.

    Deletes ``agent_config.json`` so every role reverts to defaults (the
    global ``_default`` if env-set, else the neutral local placeholder) — the
    operator then sets one endpoint fresh, with no stale per-role rows
    shadowing it. ``keep_default=True`` preserves the global ``_default`` row
    and clears only the per-role rows. Returns ``{ok, removed, path}``.
    """
    # Drop in-process caches so a reconfigure right after a reset isn't served
    # a stale local-model id / catalog from the 5-minute TTL caches.
    _LOCAL_DEFAULT_CACHE[0] = 0.0
    _LOCAL_DEFAULT_CACHE[1] = None
    with _CATALOG_LOCK:
        _CATALOG_CACHE.clear()
    _fc.clear()
    with _LOCK:
        p = _path()
        if not p.exists():
            return {"ok": True, "removed": False, "path": str(p),
                    "note": "no saved config to reset"}
        if keep_default:
            # NEVER delete the file in keep_default mode — strip only the
            # per-role rows, preserving the global _default (write back an
            # empty/`{}` map when there was no _default, so the request is
            # honoured exactly rather than nuking everything).
            try:
                disk = _fc.read_json(p) or {}
            except Exception:  # noqa: BLE001
                disk = {}
            kept = {k: v for k, v in disk.items() if k == _DEFAULT_KEY}
            _fc.write_json(p, kept)
            return {"ok": True, "removed": "per-role rows", "path": str(p),
                    "kept_default": bool(kept)}
        try:
            p.unlink()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "path": str(p)}
        return {"ok": True, "removed": True, "path": str(p)}


def resolve_litellm(role: str) -> dict[str, Any]:
    """Return the kwargs needed to build a LiteLLMModel for this role.

    Handles provider-specific prefixing, base_url, and api_key lookup
    from env / default. Callers pass the result straight into
    LiteLLMModel(**this_dict).

    Unknown roles (enhancer / validator fallback) resolve to the global
    ``_default`` via :func:`_row_for` rather than raising.
    """
    row = _row_for(role)
    prov = PROVIDERS.get(row["provider"]) or PROVIDERS["openai_compatible"]
    prefix = prov["litellm_prefix"]
    model = row["model"]
    # Cheap-tier fallback (Change 3): an unpinned cheap role (triage/enhancer)
    # uses AIFORGE_CHEAP_MODEL when set, so throwaway ops don't load the big
    # model on a serial endpoint. No-op when the env is unset.
    _cheap = cheap_model_for(role)
    if _cheap:
        model = _cheap
    # Always add LiteLLM provider prefix unless caller already supplied one.
    # mlx-lm expects the full filesystem path as ``model`` — those paths have
    # ``/`` separators, so the old "if '/' not in model" check skipped them
    # and LiteLLM raised "LLM Provider NOT provided". Detect known prefixes
    # (openai, azure, ...) instead.
    if not any(model.startswith(p) for p in KNOWN_PREFIXES):
        model = f"{prefix}/{model}"
    # Resolution order: env override > stored per-role base_url > provider
    # default. load_all() already folded env into the row, so any
    # AIFORGE_<ROLE>_BASE_URL is reflected in row["base_url"] before we
    # get here.
    stored = row.get("base_url")
    base_url = (
        os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
        or stored
        or prov.get("base_url")
    )
    # env > per-role env > stored config key > provider default
    # ("not-needed" sentinel so OSS-no-token endpoints still get a
    # non-empty key, which the OpenAI client requires).
    api_key = (
        os.environ.get(prov["api_key_env"])
        or os.environ.get(f"AIFORGE_{role.upper()}_API_KEY")
        or row.get("api_key")
        or prov["api_key_default"]
    )
    return {
        "model_id": model, "api_base": base_url, "api_key": api_key,
        # Per-role TLS opt-out for a self-signed / internal HTTPS endpoint.
        # Honoured by escalating_llm._build_one (passes ssl_verify=False to
        # LiteLLM) alongside the global AIFORGE_LLM_SSL_VERIFY toggle.
        "insecure_tls": bool(row.get("insecure_tls")),
    }


# Auto-escalation chain — preferred order when the primary fails.
# ``openai_compatible`` is the only provider and has no blind-usable
# default model, so there is no built-in cloud chain. An operator can
# still pin one via AIFORGE_<ROLE>_CLOUD_PROVIDER, but with no default
# model it is skipped. Kept empty so the chain helpers no-op gracefully.
_CLOUD_PROVIDERS_ORDERED: tuple[str, ...] = ()


def cloud_escalation_chain(role: str) -> list[dict[str, Any]]:
    """Return cloud-provider configs to try after the primary fails.

    Skips providers without an api_key (they'd just fail again) and the
    role's current primary provider (no point retrying the same thing).

    Honoured envs:
      AIFORGE_<ROLE>_CLOUD_PROVIDER  pin a single cloud target
      AIFORGE_CLOUD_PROVIDER         global cloud preference
      AIFORGE_ESCALATE_DISABLE=1     turn the chain off entirely

    The returned list mirrors :func:`resolve_litellm` shape so the runner
    can build a LiteLlm from each entry without further translation.
    """
    if os.environ.get("AIFORGE_ESCALATE_DISABLE", "0") in ("1", "true"):
        return []
    primary_provider = _row_for(role)["provider"]
    pinned = (
        os.environ.get(f"AIFORGE_{role.upper()}_CLOUD_PROVIDER")
        or os.environ.get("AIFORGE_CLOUD_PROVIDER")
    )
    candidates: list[str] = []
    if pinned:
        candidates.append(pinned.lower())
    for name in _CLOUD_PROVIDERS_ORDERED:
        if name not in candidates:
            candidates.append(name)
    out: list[dict[str, Any]] = []
    for name in candidates:
        if name == primary_provider:
            continue
        if name not in PROVIDERS:
            continue
        prov = PROVIDERS[name]
        # Skip providers we have no key for — they'd 401 immediately.
        api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
        if not api_key:
            continue
        # Build an ad-hoc resolve_litellm-shaped dict with the provider's
        # default model — caller can override via env if needed.
        prefix = prov["litellm_prefix"]
        model = prov.get("default_model")
        if not model:
            # No usable default model (e.g. openai_compatible needs a per-role
            # base_url + model). Can't blind-escalate to it — skip.
            continue
        if not any(model.startswith(p) for p in KNOWN_PREFIXES):
            model = f"{prefix}/{model}"
        base_url = (
            os.environ.get(f"AIFORGE_{role.upper()}_{name.upper()}_BASE_URL")
            or prov.get("base_url")
        )
        entry: dict[str, Any] = {
            "model_id": model, "api_base": base_url, "api_key": api_key,
            "_provider": name,
        }
        out.append(entry)
    return out


def cloud_default_for_local(role: str) -> dict[str, Any] | None:
    """Return a cloud-shaped cfg to use when the primary is unreachable.

    With ``openai_compatible`` the only provider (no blind-usable default
    model) there is no built-in cloud default, so this returns ``None``
    unless an operator pins a provider that happens to carry a default
    model. Caller keeps the primary cfg + relies on the per-call retry
    chain.
    """
    if os.environ.get("AIFORGE_ESCALATE_DISABLE", "0") in ("1", "true"):
        return None
    pinned = (
        os.environ.get(f"AIFORGE_{role.upper()}_LOCAL_DEAD_FALLBACK")
        or os.environ.get("AIFORGE_LOCAL_DEAD_FALLBACK")
    )
    candidates: list[str] = []
    if pinned:
        candidates.append(pinned.lower())
    for name in _CLOUD_PROVIDERS_ORDERED:
        if name not in candidates:
            candidates.append(name)
    for name in candidates:
        prov = PROVIDERS.get(name)
        if prov is None:
            continue
        api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
        if not api_key:
            continue
        prefix = prov["litellm_prefix"]
        model = prov.get("default_model")
        if not model:
            continue   # no usable default model → can't use as dead-local fallback
        if not any(model.startswith(p) for p in KNOWN_PREFIXES):
            model = f"{prefix}/{model}"
        entry: dict[str, Any] = {
            "model_id": model,
            "api_base": prov.get("base_url"),
            "api_key": api_key,
            "_provider": name,
        }
        return entry
    return None


# ────────────────────────── Settings UI helpers ────────────────────────


# ────────────────────────── Per-role tool scoping ──────────────────────
# Parsed from ``aiforge_core/agents/agents.yaml`` (the SAME source the ADK /
# GA / harness layers read). Lets the tool factory hand each agent only the
# tools its role is permitted — a security/scoping backstop that no longer
# relies on the model honouring a prompt contract. Soft-fail: any parse error
# → allow-all (never break the pipeline build over a malformed YAML).

_AGENTS_CONTRACTS_CACHE: dict[str, Any] = {"loaded": False, "contracts": None}


def _agent_contracts() -> "dict[str, Any] | None":
    """Lazy-load + cache the ``agents.yaml`` contracts. ``None`` on failure.

    Cached once — the YAML header states changes take effect on graph-runner
    restart only, so re-reading per call would be wasted IO.
    """
    if _AGENTS_CONTRACTS_CACHE["loaded"]:
        return _AGENTS_CONTRACTS_CACHE["contracts"]
    contracts: "dict[str, Any] | None"
    try:
        from aiforge_core.agents import loader as _loader
        contracts = _loader.load_agents()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("aiforge.agent_config").warning(
            "agents.yaml unreadable for tool enforcement (%s) — enforcement "
            "disabled (all tools allowed).", exc)
        contracts = None
    _AGENTS_CONTRACTS_CACHE["contracts"] = contracts
    _AGENTS_CONTRACTS_CACHE["loaded"] = True
    return contracts


def allowed_tools_for(role: str) -> "tuple[frozenset[str] | None, frozenset[str]]":
    """Return ``(allowed_or_None, forbidden)`` tool-name sets for ``role``.

    Parsed from ``agents.yaml``. Semantics (matches the loader/GA layer):

      * ``allowed`` absent / empty / ``["all"]`` / ``["*"]`` → ``allowed=None``
        (no allowlist restriction; every tool passes except ``forbidden``).
      * ``allowed`` = explicit list → only those tool names pass.
      * ``forbidden = ["ALL"]`` → ``allowed=frozenset()`` (an EXPLICIT empty
        allowlist: nothing passes — a hard tool-less role).
      * ``forbidden`` list → always removed, even if also in ``allowed``.
      * unknown role / missing / malformed yaml → ``(None, frozenset())``
        i.e. allow-all — the backward-compatible default so a missing config
        never suddenly restricts an existing run.

    Names are matched verbatim against the tool's function name
    (``FunctionTool.name`` == ``func.__name__``) by the caller.
    """
    contracts = _agent_contracts()
    if not contracts or role not in contracts:
        return None, frozenset()
    tools = getattr(contracts[role], "tools", None)
    if tools is None:
        return None, frozenset()
    if getattr(tools, "forbidden_is_all", False):
        # forbidden=ALL → explicit empty allowlist (zero tools).
        return frozenset(), frozenset()
    allowed_list = list(getattr(tools, "allowed", None) or [])
    forbidden = frozenset(getattr(tools, "forbidden", None) or [])
    lowered = {a.strip().lower() for a in allowed_list}
    if not allowed_list or lowered <= {"all", "*"}:
        return None, forbidden
    return frozenset(allowed_list), forbidden


def archetypes() -> list[str]:
    """The public archetype roles. Legacy aliases stay invisible."""
    return list(_ARCHETYPES)


def list_providers() -> list[dict[str, Any]]:
    """Public providers in display order."""
    return [
        {
            "id": pid,
            "label": prov["label"],
            "default_model": prov["default_model"],
        }
        for pid, prov in PROVIDERS.items()
    ]


def list_models(provider: str) -> list[dict[str, Any]]:
    """Catalog for one provider — static curated + dynamic discovery."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    return _enriched_catalog(provider)


# ────────────────────────── Profile presets ────────────────────────────
# Profiles used to bulk-switch every archetype between provider stacks
# (local / Ollama Cloud). With ``openai_compatible`` the only provider
# there is nothing meaningful to preset, so the dict is empty — the "Apply
# to all" widget (per-role bulk set) covers the same need. Kept as an empty
# dict so callers (profiles() / apply_profile() / the API) don't break.

PROFILES: dict[str, dict[str, str]] = {}


def profiles() -> list[str]:
    """Names of the bundled profiles (none — see PROFILES)."""
    return list(PROFILES.keys())


def apply_profile(name: str) -> dict[str, dict[str, Any]]:
    """Bulk-assign one (provider, model) pair to every archetype.

    No profiles are bundled anymore (``openai_compatible`` is the only
    provider). Always raises ``ValueError`` so the API surfaces a clean
    404 rather than crashing.
    """
    raise ValueError(
        f"unknown profile: {name!r}. no profiles are bundled — use "
        f"'Apply to all' on the Home page instead."
    )
