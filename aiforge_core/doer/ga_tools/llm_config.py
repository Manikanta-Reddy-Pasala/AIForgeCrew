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


def primary_cfg() -> dict:
    """Active doer backend, gated by global AIFORGE_PRIMARY_BACKEND.

    Honours the same flag every other agent honours (Planner /
    Feedback / Learner / Chat) so flipping Settings → 'gemini'
    flips them all at once.
    """
    from aiforge_core.runtime.llm_picker import pick as _pick
    ep = _pick("doer")
    if ep.backend == "gemini":
        cloud = fallback_cfg()
        if cloud is not None:
            cloud["max_retries"] = 2
            cloud["name"] = "gemini-primary"
            return cloud
        # Key missing — silent-fall back to local rather than crash.
    base_url = os.environ.get(
        "AIFORGE_DOER_BASE_URL", "http://127.0.0.1:1234"
    )
    model = os.environ.get(
        "AIFORGE_DOER_MODEL",
        "/Users/manikanta/.lmstudio/models/lmstudio-community/Qwen3-Coder-Next-MLX-4bit",
    )
    cfg: dict = {
        "name": "mlx-doer",
        "apikey": os.environ.get("AIFORGE_DOER_API_KEY", "sk-local"),
        "apibase": base_url.rstrip("/").rstrip("/v1"),
        "model": model,
        "api_mode": "chat_completions",
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
