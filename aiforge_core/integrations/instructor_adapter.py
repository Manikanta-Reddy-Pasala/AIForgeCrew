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

    cli = instructor.from_openai(
        OpenAI(base_url=base_url, api_key=api_key or "not-needed",
               timeout=timeout_s or 120),
        mode=instructor.Mode.MD_JSON)
    kwargs: dict = {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None:
        kwargs["temperature"] = temperature
    return cli.chat.completions.create(
        model=model, messages=list(messages),
        response_model=response_model, max_retries=max_retries, **kwargs)


__all__ = ["available", "structured"]
