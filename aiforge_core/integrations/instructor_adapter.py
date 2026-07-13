"""instructor adapter — Pydantic-validated structured output over an
OpenAI-compatible endpoint (``pip install aiforgecrew[structured]``).

Uses ``Mode.MD_JSON`` (schema-in-prompt + JSON extraction + auto-reask): the
one mode that works against local servers (LM Studio/MLX) that reject
``response_format: json_object``. Raises on any failure — the domain caller
(:mod:`aiforge_core.llm.structured`) owns the fallback loop.
"""
from __future__ import annotations

from pydantic import BaseModel


def available() -> bool:
    try:
        import instructor  # noqa: F401
        import openai      # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def structured(*, base_url: str, api_key: str, model: str,
               messages: list[dict], response_model: type[BaseModel],
               max_retries: int = 2, max_tokens: int | None = None,
               timeout_s: int | None = None,
               temperature: float | None = None) -> BaseModel:
    """One validated completion. Raises ImportError when the lib is missing,
    or whatever instructor raises when retries exhaust."""
    import instructor
    from openai import OpenAI

    # The OpenAI SDK builds its own httpx client that, by default, IGNORES
    # AIForge's TLS policy — so a self-hosted HTTPS/self-signed model endpoint
    # (AIFORGE_LLM_SSL_VERIFY=false / a CA bundle) fails with a bare "Connection
    # error" while the litellm fallback connects. Hand OpenAI an httpx client
    # using the same verify policy litellm uses.
    _http = None
    try:
        import httpx
        from aiforge_core.net.ssl import httpx_verify
        _http = httpx.Client(verify=httpx_verify(base_url),
                             timeout=timeout_s or 120)
    except Exception:  # noqa: BLE001 — fall back to the SDK default client
        _http = None
    _oai_kwargs = {"base_url": base_url, "api_key": api_key or "not-needed",
                   "timeout": timeout_s or 120}
    if _http is not None:
        _oai_kwargs["http_client"] = _http
    cli = instructor.from_openai(OpenAI(**_oai_kwargs),
                                 mode=instructor.Mode.MD_JSON)
    import os
    kwargs: dict = {}
    # A structured (JSON) reply truncated by a too-small max_tokens raises
    # IncompleteOutputException and forces the fallback loop (noisy + a wasted
    # call). Give it a sensible FLOOR so short structured extractions don't get
    # clipped — tunable via AIFORGE_STRUCTURED_MAX_TOKENS.
    try:
        _floor = max(256, int(os.environ.get("AIFORGE_STRUCTURED_MAX_TOKENS", "4096")))
    except (TypeError, ValueError):
        _floor = 4096
    kwargs["max_tokens"] = max(int(max_tokens), _floor) if max_tokens else _floor
    if temperature is not None:
        kwargs["temperature"] = temperature
    return cli.chat.completions.create(
        model=model, messages=list(messages),
        response_model=response_model, max_retries=max_retries, **kwargs)


__all__ = ["available", "structured"]
