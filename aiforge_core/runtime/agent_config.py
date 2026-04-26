"""Per-agent model + provider config, persisted to a JSON file.

Ops can swap any role (supervisor/planner/doer/feedback/learner/chat)
between local LM Studio, Anthropic Claude, or Ollama Cloud without a
redeploy. Env vars still override for single-box debug runs.

Storage:  $AIFORGE_CONFIG_DIR/agent_config.json  (default ~/.aiforge).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

_ROLES = ("supervisor", "planner", "doer", "feedback", "learner", "chat")

PROVIDERS: dict[str, dict[str, Any]] = {
    "local": {
        "label": "LM Studio (local)",
        # base_url overridden at read time from AIFORGE_LM_BASE_URL.
        "litellm_prefix": "openai",
        "default_model": "gpt-oss-120b",
        "api_key_env": "LM_STUDIO_API_KEY",
        "api_key_default": "lm-studio",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "litellm_prefix": "anthropic",
        "default_model": "claude-sonnet-4-5",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key_default": "",
        "base_url": None,  # LiteLLM uses its native Anthropic handler.
    },
    "ollama_cloud": {
        "label": "Ollama Cloud",
        "litellm_prefix": "openai",
        "default_model": "llama3.1:70b",
        "api_key_env": "OLLAMA_CLOUD_API_KEY",
        "api_key_default": "",
        "base_url": "https://ollama.com/v1",
    },
}

_DEFAULT: dict[str, dict[str, str]] = {
    role: {"provider": "local", "model": "gpt-oss-120b"} for role in _ROLES
}
# Chat ships on Ollama Cloud by default — local mlx-lm is too slow for
# the live Q+A flow and gemini is parked behind a hidden flag while we
# standardise on Ollama. Flip via the Settings UI or
# AIFORGE_CHAT_PROVIDER env.
_DEFAULT["chat"] = {"provider": "ollama_cloud", "model": "llama3.1:70b"}


def _path() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "agent_config.json"


def load_all() -> dict[str, dict[str, str]]:
    """Read the full per-role map, merging defaults for missing keys."""
    p = _path()
    cfg = dict(_DEFAULT)
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
                        }
        except Exception:
            pass
    # Env override: AIFORGE_<ROLE>_MODEL / AIFORGE_<ROLE>_PROVIDER.
    for role in _ROLES:
        env_model = os.environ.get(f"AIFORGE_{role.upper()}_MODEL")
        env_prov = os.environ.get(f"AIFORGE_{role.upper()}_PROVIDER")
        if env_model:
            cfg[role]["model"] = env_model
        if env_prov:
            cfg[role]["provider"] = env_prov
    return cfg


def get(role: str) -> dict[str, str]:
    """Return resolved config for one role."""
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    return load_all()[role]


def set_role(role: str, provider: str, model: str) -> dict[str, str]:
    """Persist {provider, model} for a single role. Env vars still win on
    next read, which is desired for a one-off override without losing the
    saved default."""
    if role not in _ROLES:
        raise ValueError(f"unknown role: {role}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    if not model or not model.strip():
        raise ValueError("model cannot be empty")
    with _LOCK:
        p = _path()
        disk: dict[str, dict[str, str]] = {}
        if p.exists():
            try:
                disk = json.loads(p.read_text()) or {}
            except Exception:
                disk = {}
        disk[role] = {"provider": provider, "model": model.strip()}
        p.write_text(json.dumps(disk, indent=2))
    return get(role)


def resolve_litellm(role: str) -> dict[str, Any]:
    """Return the kwargs needed to build a LiteLLMModel for this role.

    Handles provider-specific prefixing, base_url, and api_key lookup
    from env / default. Callers pass the result straight into
    LiteLLMModel(**this_dict).
    """
    row = get(role)
    prov = PROVIDERS.get(row["provider"]) or PROVIDERS["local"]
    prefix = prov["litellm_prefix"]
    model = row["model"]
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
    base_url = prov.get("base_url")
    if row["provider"] == "local":
        # Per-role override → global override → default. Lets us run
        # one mlx-lm server per role on different ports (planner=1235,
        # doer=1234) since mlx-lm only serves one model per process.
        base_url = (
            os.environ.get(f"AIFORGE_{role.upper()}_BASE_URL")
            or os.environ.get("AIFORGE_LM_BASE_URL")
            or "http://127.0.0.1:1234/v1"
        )
    api_key = os.environ.get(prov["api_key_env"]) or prov["api_key_default"]
    return {
        "model_id": model, "api_base": base_url, "api_key": api_key,
    }
