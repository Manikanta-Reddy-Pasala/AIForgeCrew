"""Per-archetype model + provider config, persisted to a JSON file.

The 6 archetypes match the v5 production pipeline (see
``aiforge_core/agents/agents.yaml`` + ``runtime.adk_runner``):

    architect, planner, verifier, doer, feedback, learner

Architect is external (human-driven Claude Code) but still configurable
here for trace symmetry — its model pin is read by the operator's
external client. The other five run inside the ADK SequentialAgent:
``Planner → Verifier → LoopAgent[Doer, Feedback] → Learner``.

Each archetype can be flipped between providers (local mlx_lm / Ollama
Cloud / Anthropic Claude / Claude subscription CLI) without a redeploy.
Env vars still override at read time so ops keeps a final-say escape
hatch.

Storage: ``$AIFORGE_CONFIG_DIR/agent_config.json`` (default ``~/.aiforge``).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
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
)
_ROLES = _ARCHETYPES

# Local default — resolved dynamically, in order:
#   1. AIFORGE_LOCAL_DEFAULT_MODEL env (operator pin)
#   2. first model id served by the local /v1/models endpoint (5-min cache)
#   3. legacy hardcoded path (only when the server is unreachable AND no
#      env pin exists — keeps cold-start behavior identical to before)
_LOCAL_FALLBACK_MODEL = (
    "/Users/manikanta/.lmstudio/models/lmstudio-community/"
    "Qwen3-Coder-Next-MLX-4bit"
)
_LOCAL_DEFAULT_CACHE: list[Any] = [0.0, None]  # [ts, model_id]
_LOCAL_DEFAULT_TTL_S = 300.0


def _local_default_model() -> str:
    """Resolve the local provider's default model id at call time."""
    env = os.environ.get("AIFORGE_LOCAL_DEFAULT_MODEL")
    if env and env.strip():
        return env.strip()
    now = time.time()
    if _LOCAL_DEFAULT_CACHE[1] and (now - _LOCAL_DEFAULT_CACHE[0]) < \
            _LOCAL_DEFAULT_TTL_S:
        return _LOCAL_DEFAULT_CACHE[1]
    try:
        discovered = _discover_local_models()
    except Exception:
        discovered = []
    if discovered:
        _LOCAL_DEFAULT_CACHE[0] = now
        _LOCAL_DEFAULT_CACHE[1] = discovered[0]["id"]
        return _LOCAL_DEFAULT_CACHE[1]
    return _LOCAL_FALLBACK_MODEL

PROVIDERS: dict[str, dict[str, Any]] = {
    "local": {
        "label": "Local (LM Studio / mlx-lm on Mac Studio)",
        "litellm_prefix": "openai",
        # Resolved at call time by _local_default_model() — see
        # list_providers() / _defaults(). None here means "dynamic".
        "default_model": None,
        "api_key_env": "LM_STUDIO_API_KEY",
        "api_key_default": "lm-studio",
    },
    # Generic OpenAI-compatible endpoint — the deploy-anywhere provider.
    # User supplies base_url (+ optional api_key) per role via the home
    # page. Covers OSS-no-token, LM Studio, OpenRouter, Groq, Together,
    # vLLM, and cloud-with-key. Blank key = no token.
    "openai_compatible": {
        "label": "OpenAI-compatible (any base URL — OSS / LM Studio / OpenRouter / cloud)",
        "litellm_prefix": "openai",
        "default_model": None,          # free-text / discovered via /v1/models
        "api_key_env": "AIFORGE_OPENAI_COMPAT_API_KEY",
        "api_key_default": "not-needed",  # OSS endpoints ignore the key
        "base_url": None,               # required per-role (UI-set)
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "litellm_prefix": "anthropic",
        "default_model": "claude-sonnet-4-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key_default": "",
        "base_url": None,
    },
    "ollama_cloud": {
        "label": "Ollama Cloud",
        "litellm_prefix": "openai",
        # Colon-delimited model ids (e.g. ``llama3.1:70b``) trigger
        # LiteLLM's Ollama auto-detector even with the ``openai/`` prefix
        # and route to ``/api/generate`` instead of the OpenAI-compat
        # ``/chat/completions`` ollama.com actually exposes. Pin a name
        # without ``:`` so LiteLLM stays on the openai code path.
        "default_model": "qwen3-coder-next",
        "api_key_env": "OLLAMA_CLOUD_API_KEY",
        "api_key_default": "",
        "base_url": "https://ollama.com/v1",
    },
    # Claude subscription via `claude` CLI subprocess — no API billing.
    # base_url marker `claude:cli` signals the runtime to skip LiteLLM and
    # shell out via `aiforge_core.llm.client._send_via_claude_cli`.
    "claude_local": {
        "label": "Claude Subscription (CLI)",
        "litellm_prefix": "anthropic",
        "default_model": "claude-opus-4-7",
        "api_key_env": "AIFORGE_CLAUDE_API_KEY",   # unused; kept for parity
        "api_key_default": "",
        "base_url": "claude:cli",
    },
}

def _defaults() -> dict[str, dict[str, Any]]:
    """Per-role defaults, resolved lazily so the local model id tracks
    whatever the local server actually serves (no hardcoded pin)."""
    model = _local_default_model()
    return {
        role: {"provider": "local", "model": model, "base_url": None}
        for role in _ROLES
    }


# ────────────────────────── Model catalog ──────────────────────────────
# Hardcoded curated catalog. Local + ollama_cloud get enriched at call
# time by hitting the respective /v1/models endpoint (best-effort, with
# a short cache to avoid hammering the upstream on every UI refresh).

MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    # local is fully dynamic — populated by _discover_local_models()
    # hitting /v1/models on the configured endpoints. No hardcoded
    # paths: stale entries for deleted models caused phantom catalog
    # rows (Qwen3.6-27B / gemma-4-31b were removed from disk long ago).
    "local": [],
    # openai_compatible is fully dynamic too — the model list depends on
    # the per-role base_url, so the UI fetches it on demand via the
    # provider-probe endpoint rather than from this static catalog.
    "openai_compatible": [],
    "ollama_cloud": [
        {"id": "qwen3-coder:480b", "label": "Qwen3 Coder 480B",
         "context": 128000, "tier": "premium"},
        {"id": "glm-4.7", "label": "GLM 4.7",
         "context": 128000, "tier": "premium"},
        {"id": "gpt-oss:120b", "label": "GPT-OSS 120B",
         "context": 128000, "tier": "balanced"},
        {"id": "kimi-k2:1t", "label": "Kimi K2 1T",
         "context": 128000, "tier": "premium"},
        {"id": "deepseek-v3.2", "label": "DeepSeek V3.2",
         "context": 128000, "tier": "premium"},
        {"id": "gemma4:31b", "label": "Gemma 4 31B",
         "context": 64000, "tier": "balanced"},
    ],
    "anthropic": [
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7",
         "context": 200000, "tier": "premium"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6",
         "context": 200000, "tier": "balanced"},
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5",
         "context": 200000, "tier": "fast"},
    ],
    "claude_local": [
        {"id": "claude-opus-4-7", "label": "Claude Opus 4.7 (subscription)",
         "context": 1000000, "tier": "premium"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (subscription)",
         "context": 200000, "tier": "balanced"},
        {"id": "claude-haiku-4-5-20251001",
         "label": "Claude Haiku 4.5 (subscription)",
         "context": 200000, "tier": "fast"},
    ],
}

# Module-level cache for dynamic model discovery. Keyed by provider id;
# value is (timestamp, list_of_model_dicts). 5-minute TTL.
_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CATALOG_TTL_S = 300.0
_CATALOG_LOCK = threading.Lock()


def _http_get_json(url: str, *, headers: dict[str, str] | None = None,
                   timeout: float = 3.0) -> dict | None:
    """Tiny helper. Returns parsed JSON dict on 200, None on any failure.
    Uses urllib so we add no new deps."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.getcode() != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, TimeoutError):
        return None


def _discover_local_models() -> list[dict[str, Any]]:
    """Probe the two LM Studio ports we run on Mac Studio.

    Doer typically lives on :1234, planner on :1235 (mlx-lm only serves
    one model per process). Each /v1/models response gives us the
    actual ids the server will accept. Failures fall through silently —
    the static catalog is good enough.
    """
    bases: list[str] = []
    primary = os.environ.get("AIFORGE_LM_BASE_URL", "http://127.0.0.1:1234")
    bases.append(primary.rstrip("/").rstrip("/v1"))
    # Always also probe the planner port unless the caller's primary is
    # already 1235.
    if "1235" not in primary:
        bases.append("http://127.0.0.1:1235")

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for base in bases:
        data = _http_get_json(f"{base}/v1/models") or {}
        for m in data.get("data") or []:
            mid = m.get("id") or m.get("name")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            label = mid.split("/")[-1] if "/" in mid else mid
            out.append({
                "id": mid, "label": label,
                "context": m.get("context_length"),
                "tier": "balanced",
            })
    return out


def _discover_ollama_cloud_models() -> list[dict[str, Any]]:
    """Hit Ollama Cloud's /v1/models endpoint when an API key is set."""
    api_key = os.environ.get("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        return []
    data = _http_get_json(
        "https://ollama.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=4.0,
    ) or {}
    out: list[dict[str, Any]] = []
    for m in data.get("data") or data.get("models") or []:
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        out.append({
            "id": mid,
            "label": m.get("display_name") or mid,
            "context": m.get("context_length") or m.get("context"),
            "tier": "balanced",
        })
    return out


def _enriched_catalog(provider: str) -> list[dict[str, Any]]:
    """Static curated list, optionally augmented with dynamically
    discovered models. 5-minute cache. Failures keep the static list."""
    static = list(MODEL_CATALOG.get(provider) or [])
    if provider not in ("local", "ollama_cloud"):
        return static
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(provider)
        now = time.time()
        if cached and (now - cached[0]) < _CATALOG_TTL_S:
            return cached[1]
        try:
            if provider == "local":
                discovered = _discover_local_models()
            else:
                discovered = _discover_ollama_cloud_models()
        except Exception:
            discovered = []
        # Merge: static first (curated order preserved), then any
        # discovered ids we don't already know about.
        known = {m["id"] for m in static}
        merged = list(static)
        for m in discovered:
            if m["id"] not in known:
                merged.append(m)
                known.add(m["id"])
        _CATALOG_CACHE[provider] = (now, merged)
        return merged


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
    if p.exists():
        try:
            disk = json.loads(p.read_text())
            if isinstance(disk, dict):
                for role, row in disk.items():
                    if role in _ROLES and isinstance(row, dict):
                        cfg[role] = {
                            "provider": row.get("provider") or
                                        cfg[role]["provider"],
                            "model": row.get("model") or cfg[role]["model"],
                            "base_url": row.get("base_url"),
                            "api_key": row.get("api_key"),
                        }
        except Exception:
            pass
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
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    return load_all()[role]


def set_role(role: str, provider: str, model: str,
             base_url: str | None = None,
             api_key: str | None = None) -> dict[str, Any]:
    """Persist {provider, model, base_url?, api_key?} for a single role.

    ``base_url`` is optional; when None, the provider's default is used at
    resolve time. ``api_key`` is optional too — used mainly by the
    ``openai_compatible`` provider for cloud-with-key endpoints; leave it
    blank for OSS-no-token. Env vars still win on next read, which is
    desired for a one-off override without losing the saved default.
    """
    if role not in _ROLES:
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
                disk = json.loads(p.read_text()) or {}
            except Exception:
                disk = {}
        row: dict[str, Any] = {
            "provider": provider, "model": model.strip(),
        }
        if base_url and base_url.strip():
            row["base_url"] = base_url.strip()
        else:
            row["base_url"] = None
        if api_key and api_key.strip():
            row["api_key"] = api_key.strip()
        else:
            row["api_key"] = None
        disk[role] = row
        p.write_text(json.dumps(disk, indent=2))
    return get(role)


def resolve_litellm(role: str) -> dict[str, Any]:
    """Return the kwargs needed to build a LiteLLMModel for this role.

    Handles provider-specific prefixing, base_url, and api_key lookup
    from env / default. Callers pass the result straight into
    LiteLLMModel(**this_dict).

    For ``claude_local`` the returned dict carries ``api_base="claude:cli"``
    and an extra key ``_claude_cli=True``. Runtimes MUST detect this and
    route the call through ``aiforge_core.llm.client._send_via_claude_cli``
    instead of constructing a LiteLLMModel.
    """
    row = get(role)
    prov = PROVIDERS.get(row["provider"]) or PROVIDERS["local"]
    prefix = prov["litellm_prefix"]
    model = row["model"]
    # Claude subscription: no LiteLLM, no API key. Runtime must subprocess.
    if row["provider"] == "claude_local":
        return {
            "model_id": model,
            "api_base": "claude:cli",
            "api_key": "",
            "_claude_cli": True,
        }
    # Always add LiteLLM provider prefix unless caller already supplied one.
    # mlx-lm expects the full filesystem path as ``model`` — those paths have
    # ``/`` separators, so the old "if '/' not in model" check skipped them
    # and LiteLLM raised "LLM Provider NOT provided". Detect known prefixes
    # (openai, anthropic, ...) instead.
    KNOWN_PREFIXES = (
        "openai/", "anthropic/", "azure/", "ollama/", "huggingface/",
        "mistral/", "groq/", "cohere/", "bedrock/",
    )
    if not any(model.startswith(p) for p in KNOWN_PREFIXES):
        model = f"{prefix}/{model}"
    # Resolution order: env override > stored per-role base_url > provider
    # default. load_all() already folded env into the row, so any
    # AIFORGE_<ROLE>_BASE_URL is reflected in row["base_url"] before we
    # get here.
    stored = row.get("base_url")
    base_url = stored or prov.get("base_url")
    if row["provider"] == "local":
        # Per-role override → global override → stored → default. Lets us
        # run one mlx-lm server per role on different ports (planner=1235,
        # doer=1234) since mlx-lm only serves one model per process.
        base_url = (
            os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
            or stored
            or os.environ.get("AIFORGE_LM_BASE_URL")
            or "http://127.0.0.1:1234/v1"
        )
    elif row["provider"] == "ollama_cloud":
        base_url = (
            os.environ.get(f"AIFORGE_{role.upper()}_OLLAMA_CLOUD_BASE_URL")
            or os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
            or stored
            or os.environ.get("AIFORGE_OLLAMA_CLOUD_BASE_URL")
            or prov.get("base_url")
        )
    else:
        base_url = (
            os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
            or stored
            or prov.get("base_url")
        )
    if row["provider"] == "openai_compatible":
        # env > per-role env > stored config key > provider default
        # ("not-needed" sentinel so OSS-no-token endpoints still get a
        # non-empty key, which the OpenAI client requires).
        api_key = (
            os.environ.get(prov["api_key_env"])
            or os.environ.get(f"AIFORGE_{role.upper()}_API_KEY")
            or row.get("api_key")
            or prov["api_key_default"]
        )
    else:
        api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
    return {
        "model_id": model, "api_base": base_url, "api_key": api_key,
    }


# Auto-escalation chain — preferred order when the primary fails.
# Cloud providers only; runtime falls through to the first one with a
# usable api_key. claude_local sits last because it's slowest and most
# expensive in subscription quota.
_CLOUD_PROVIDERS_ORDERED: tuple[str, ...] = (
    "ollama_cloud", "anthropic", "claude_local",
)


def cloud_escalation_chain(role: str) -> list[dict[str, Any]]:
    """Return cloud-provider configs to try after the primary fails.

    Skips providers without an api_key (they'd just fail again) and the
    role's current primary provider (no point retrying the same thing).

    Honoured envs:
      AIFORGE_<ROLE>_CLOUD_PROVIDER  pin a single cloud target
      AIFORGE_CLOUD_PROVIDER         global cloud preference
      AIFORGE_ESCALATE_DISABLE=1     turn the chain off entirely

    The returned list mirrors :func:`resolve_litellm` shape so the runner
    can build a LiteLlm or ClaudeSubscriptionLlm from each entry without
    further translation.
    """
    if os.environ.get("AIFORGE_ESCALATE_DISABLE", "0") in ("1", "true"):
        return []
    primary_provider = get(role)["provider"]
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
        # claude_local is the exception: the CLI reads the OS keychain,
        # not an env var, so we can't pre-flight-validate it here.
        api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
        if name != "claude_local" and not api_key:
            continue
        # Build an ad-hoc resolve_litellm-shaped dict with the provider's
        # default model — caller can override via env if needed.
        if name == "claude_local":
            out.append({
                "model_id": prov["default_model"],
                "api_base": "claude:cli",
                "api_key": "",
                "_claude_cli": True,
                "_provider": name,
            })
            continue
        prefix = prov["litellm_prefix"]
        model = prov["default_model"]
        KNOWN_PREFIXES = (
            "openai/", "anthropic/", "azure/", "ollama/", "huggingface/",
            "mistral/", "groq/", "cohere/", "bedrock/",
        )
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
        # Ollama Cloud sits at https://ollama.com/v1 (OpenAI-compat) but
        # LiteLLM detects the ollama.com domain + treats it as an Ollama
        # endpoint, posting to /api/generate which 404s. Force the
        # openai code path explicitly.
        if name == "ollama_cloud":
            entry["custom_llm_provider"] = "openai"
        out.append(entry)
    return out


def cloud_default_for_local(role: str) -> dict[str, Any] | None:
    """Return a cloud-shaped cfg to use when the ``local`` primary is
    unreachable.

    Picked the same way :func:`cloud_escalation_chain` picks chain
    entries — first cloud provider in
    ``ollama_cloud → anthropic → claude_local`` order that has a key
    configured. Honours the per-role / global pin envs so an operator
    can force ``anthropic`` here instead of the default Ollama Cloud.

    Returns ``None`` when no cloud is configured (caller keeps the
    dead local cfg + relies on the chain to rescue per-call).
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
        if name != "claude_local" and not api_key:
            continue
        if name == "claude_local":
            return {
                "model_id": prov["default_model"],
                "api_base": "claude:cli",
                "api_key": "",
                "_claude_cli": True,
                "_provider": name,
            }
        prefix = prov["litellm_prefix"]
        model = prov["default_model"]
        KNOWN_PREFIXES = (
            "openai/", "anthropic/", "azure/", "ollama/", "huggingface/",
            "mistral/", "groq/", "cohere/", "bedrock/",
        )
        if not any(model.startswith(p) for p in KNOWN_PREFIXES):
            model = f"{prefix}/{model}"
        entry: dict[str, Any] = {
            "model_id": model,
            "api_base": prov.get("base_url"),
            "api_key": api_key,
            "_provider": name,
        }
        if name == "ollama_cloud":
            entry["custom_llm_provider"] = "openai"
        return entry
    return None


# ────────────────────────── Settings UI helpers ────────────────────────


def archetypes() -> list[str]:
    """The 9 public archetype roles. Legacy aliases stay invisible."""
    return list(_ARCHETYPES)


def list_providers() -> list[dict[str, Any]]:
    """Public providers in display order."""
    return [
        {
            "id": pid,
            "label": prov["label"],
            "default_model": (prov["default_model"]
                              or (_local_default_model()
                                  if pid == "local" else None)),
        }
        for pid, prov in PROVIDERS.items()
    ]


def list_models(provider: str) -> list[dict[str, Any]]:
    """Catalog for one provider — static curated + dynamic discovery."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    return _enriched_catalog(provider)


# ────────────────────────── Profile presets ────────────────────────────
# A profile assigns one (provider, model) pair to all 9 archetypes at
# once — the "give me a full claude_local stack" or "everything on
# Ollama Cloud" knob. After applying, individual archetypes can still
# be flipped via set_role() or env vars (mix-and-match).

PROFILES: dict[str, dict[str, str]] = {
    # Full Claude subscription stack — every archetype shells out to
    # `claude` CLI on the keychain host. NUC -> Mac Studio works via
    # AIFORGE_CLAUDE_HOST=mac-studio (ssh).
    "claude_local": {
        "provider": "claude_local",
        "model": "claude-opus-4-7",
    },
    # Full Ollama Cloud stack — paid hosted, ~128K ctx, Qwen3-Coder 480B.
    "ollama_cloud": {
        "provider": "ollama_cloud",
        "model": "qwen3-coder:480b",
    },
    # Full local stack — single LM Studio process serves all archetypes.
    # Tune AIFORGE_LM_BASE_URL (default 1234) for per-role port routing.
    # model="" means "resolve dynamically at apply time" — see
    # apply_profile().
    "local": {
        "provider": "local",
        "model": "",
    },
}


def profiles() -> list[str]:
    """Names of the bundled profiles."""
    return list(PROFILES.keys())


def apply_profile(name: str) -> dict[str, dict[str, Any]]:
    """Bulk-assign one (provider, model) pair to every archetype.

    Returns the resulting per-role config map. Existing per-role base_url
    overrides are cleared — the profile is meant as a clean slate. After
    applying, callers can still call ``set_role()`` to mix-and-match.

    Raises ``ValueError`` on unknown profile name.
    """
    if name not in PROFILES:
        raise ValueError(
            f"unknown profile: {name!r}. known: {sorted(PROFILES)}"
        )
    spec = PROFILES[name]
    model = spec["model"] or _local_default_model()
    out: dict[str, dict[str, Any]] = {}
    for role in _ARCHETYPES:
        out[role] = set_role(role, spec["provider"], model)
    return out
