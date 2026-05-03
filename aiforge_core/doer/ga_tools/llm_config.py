"""LLM session config for the Doer.

Two cfg builders:

* ``primary_cfg``: local mlx-lm on :1234, the everyday Doer model.
* ``fallback_cfg``: Gemini 2.5-flash via Google AI Studio when
  ``AIFORGE_GOOGLE_API_KEY`` is set. Used when the primary errors
  out (network, OOM, garbled tool emission). Free-tier quota
  generous enough for a few stuck tickets per day.

GA's ``MixinSession`` (llmcore.py) consumes a list of sessions and
auto-springs back to primary after a configurable cool-off, so we
don't permanently park the doer on Gemini after one mlx-lm hiccup.
"""
from __future__ import annotations

import os


def primary_cfg(tools: list[dict] | None = None) -> dict:
    """Active doer backend, gated by global AIFORGE_PRIMARY_BACKEND.

    Honours the same flag every other agent honours (Planner /
    Feedback / Learner / Chat) so flipping Settings → 'gemini'
    flips them all at once.

    2026-05 — accepts an optional ``tools`` schema. When provided, it is
    threaded into the cfg so GA's ``LLMSession`` includes a ``tools`` array
    in the chat.completions payload (and emits native ``message.tool_calls``
    instead of text content). Provider is read from ``agent_config`` so
    ``tool_choice="required"`` is forced for local mlx-lm / LM Studio /
    Ollama Cloud where it's known to work.
    """
    from aiforge_core.runtime.llm_picker import pick as _pick
    ep = _pick("doer")
    # Resolve provider once — cheap, used to decide tool_choice.
    try:
        from aiforge_core.runtime import agent_config as _acfg
        provider = (_acfg.get("doer") or {}).get("provider") or "local"
    except Exception:
        provider = "local"
    if ep.backend == "gemini":
        cloud = fallback_cfg()
        if cloud is not None:
            cloud["max_retries"] = 2
            cloud["name"] = "gemini-primary"
            if tools:
                cloud["tools"] = tools
                cloud["tool_choice"] = os.environ.get("AIFORGE_DOER_TOOL_CHOICE", "auto")
            return cloud
        # Key missing — silent-fall back to local rather than crash.
    # 2026-05 — single source of truth = agent_config.resolve_litellm.
    # Env overrides win at agent_config.load_all level, so AIFORGE_DOER_*
    # still escape-hatches without bypassing the Settings UI.
    try:
        from aiforge_core.runtime import agent_config as _acfg
        resolved = _acfg.resolve_litellm("doer")
        raw_model = resolved.get("model_id") or ""
        for p in ("openai/", "anthropic/", "ollama/"):
            if raw_model.startswith(p):
                raw_model = raw_model[len(p):]
                break
        base_url = (resolved.get("api_base") or "http://127.0.0.1:1234/v1")
        model = raw_model or os.environ.get(
            "AIFORGE_DOER_MODEL",
            "/Users/manikanta/.lmstudio/models/lmstudio-community/Qwen3-Coder-Next-MLX-4bit",
        )
        api_key = resolved.get("api_key") or os.environ.get(
            "AIFORGE_DOER_API_KEY", "sk-local"
        )
    except Exception:
        base_url = os.environ.get(
            "AIFORGE_DOER_BASE_URL", "http://127.0.0.1:1234"
        )
        model = os.environ.get(
            "AIFORGE_DOER_MODEL",
            "/Users/manikanta/.lmstudio/models/lmstudio-community/Qwen3-Coder-Next-MLX-4bit",
        )
        api_key = os.environ.get("AIFORGE_DOER_API_KEY", "sk-local")
    cfg: dict = {
        "name": "mlx-doer",
        "apikey": api_key,
        "apibase": base_url.rstrip("/").rstrip("/v1"),
        "model": model,
        "api_mode": "chat_completions",
        # SSE streaming sometimes drops `tool_calls` chunks for
        # smaller models / cloud relays — same root as the chat
        # 0-tool issue we hit before. Force non-stream so the
        # response comes back as one JSON blob with structured
        # tool_calls intact. Override via AIFORGE_DOER_STREAM=1.
        "stream": os.environ.get("AIFORGE_DOER_STREAM", "0") == "1",
        "max_retries": 2,
        "connect_timeout": 10,
        "read_timeout": 180,
        "context_win": int(os.environ.get("AIFORGE_DOER_CTX", "28000")),
        "max_tokens": int(os.environ.get("AIFORGE_DOER_MAX_TOKENS", "8192")),
        "temperature": float(os.environ.get("AIFORGE_DOER_TEMP", "0.2")),
    }
    if os.environ.get("AIFORGE_DOER_TOP_P"):
        cfg["top_p"] = float(os.environ["AIFORGE_DOER_TOP_P"])
    if os.environ.get("AIFORGE_DOER_TOP_K"):
        cfg["top_k"] = int(os.environ["AIFORGE_DOER_TOP_K"])
    if os.environ.get("AIFORGE_DOER_THINK") == "1":
        cfg["chat_template_kwargs"] = {"enable_thinking": True}
    elif os.environ.get("AIFORGE_DOER_THINK") == "0":
        cfg["chat_template_kwargs"] = {"enable_thinking": False}
    # 2026-05 — pass tools straight through to GA's LLMSession so the
    # OpenAI-compat chat.completions payload carries the tools array.
    # Without this, LM Studio and Ollama Cloud emit text content with
    # no ``tool_calls[]`` (ticket ONE-85: 4 LLM calls × 0 tools each).
    if tools:
        cfg["tools"] = tools
        if provider in ("local", "ollama_cloud"):
            cfg["tool_choice"] = os.environ.get("AIFORGE_DOER_TOOL_CHOICE", "required")
        elif provider == "openai":
            cfg["tool_choice"] = os.environ.get("AIFORGE_DOER_TOOL_CHOICE", "auto")
        # Anthropic / others — leave tool_choice unset, GA's NativeClaude
        # path still works via its own tools-injection flow.
        if os.environ.get("AIFORGE_DOER_JSON_MODE", "0") == "1":
            ml = (model or "").lower()
            if any(k in ml for k in ("qwen", "gemma-4", "gpt-oss", "mistral")):
                cfg["response_format"] = {"type": "json_object"}
    return cfg


def fallback_cfg() -> dict | None:
    """Gemini 2.5-flash via google AI Studio. None when key missing."""
    api_key = os.environ.get("AIFORGE_GOOGLE_API_KEY", "")
    if not api_key:
        return None
    return {
        "name": "gemini-fallback",
        "apikey": api_key,
        "apibase": (
            "https://generativelanguage.googleapis.com/v1beta/openai"
        ),
        "model": "gemini-2.5-flash",
        "api_mode": "chat_completions",
        "max_retries": 1,
        "connect_timeout": 10,
        "read_timeout": 180,
        "context_win": 200000,
        "max_tokens": 16384,
        "temperature": 0.2,
    }
