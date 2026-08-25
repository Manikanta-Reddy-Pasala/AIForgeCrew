"""LiteLLM resolution + cloud escalation chains for ``agent_config``
(split submodule)."""
from __future__ import annotations

import os
from typing import Any

from ._resolve import _row_for, cheap_model_for
from ._state import PROVIDERS

# LiteLLM provider prefixes we leave untouched on a model id (already
# namespaced). One shared copy — resolve_litellm + the two cloud helpers
# all use it. ``anthropic/`` dropped with the provider purge.
KNOWN_PREFIXES = (
    "openai/", "azure/", "ollama/", "huggingface/",
    "mistral/", "groq/", "cohere/", "bedrock/",
)


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


def _escalation_candidates(role: str) -> list[str]:
    """Provider names to try, pinned-first then the default order, deduped."""
    pinned = (os.environ.get(f"AIFORGE_{role.upper()}_CLOUD_PROVIDER")
              or os.environ.get("AIFORGE_CLOUD_PROVIDER"))
    candidates: list[str] = [pinned.lower()] if pinned else []
    for name in _CLOUD_PROVIDERS_ORDERED:
        if name not in candidates:
            candidates.append(name)
    return candidates


def _escalation_entry(name: str, role: str, primary_provider: str) -> "dict | None":
    """A resolve_litellm-shaped cfg for one candidate provider, or None to skip
    it (it IS the primary, unknown, keyless, or has no blind-usable default
    model)."""
    if name == primary_provider or name not in PROVIDERS:
        return None
    prov = PROVIDERS[name]
    # Skip providers we have no key for — they'd 401 immediately.
    api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
    if not api_key:
        return None
    model = prov.get("default_model")
    if not model:
        # No usable default model (e.g. openai_compatible needs a per-role
        # base_url + model). Can't blind-escalate to it — skip.
        return None
    if not any(model.startswith(p) for p in KNOWN_PREFIXES):
        model = f"{prov['litellm_prefix']}/{model}"
    base_url = (os.environ.get(f"AIFORGE_{role.upper()}_{name.upper()}_BASE_URL")
                or prov.get("base_url"))
    return {"model_id": model, "api_base": base_url, "api_key": api_key,
            "_provider": name}


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
    out: list[dict[str, Any]] = []
    for name in _escalation_candidates(role):
        entry = _escalation_entry(name, role, primary_provider)
        if entry is not None:
            out.append(entry)
    return out


def _dead_local_candidates(role: str) -> list[str]:
    """Provider names for the dead-primary fallback, pinned-first then default
    order, deduped."""
    pinned = (os.environ.get(f"AIFORGE_{role.upper()}_LOCAL_DEAD_FALLBACK")
              or os.environ.get("AIFORGE_LOCAL_DEAD_FALLBACK"))
    candidates: list[str] = [pinned.lower()] if pinned else []
    for name in _CLOUD_PROVIDERS_ORDERED:
        if name not in candidates:
            candidates.append(name)
    return candidates


def _dead_local_entry(name: str) -> "dict | None":
    """A cloud-shaped cfg for one fallback provider, or None to skip (unknown,
    keyless, or no blind-usable default model)."""
    prov = PROVIDERS.get(name)
    if prov is None:
        return None
    api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
    if not api_key:
        return None
    model = prov.get("default_model")
    if not model:
        return None   # no usable default model → can't use as dead-local fallback
    if not any(model.startswith(p) for p in KNOWN_PREFIXES):
        model = f"{prov['litellm_prefix']}/{model}"
    return {"model_id": model, "api_base": prov.get("base_url"),
            "api_key": api_key, "_provider": name}


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
    for name in _dead_local_candidates(role):
        entry = _dead_local_entry(name)
        if entry is not None:
            return entry
    return None
