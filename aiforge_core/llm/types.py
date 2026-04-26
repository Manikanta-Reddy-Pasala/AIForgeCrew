"""Type definitions for the LLM layer.

Keep concrete provider classes out of this module — they live in
``aiforge_core.llm.providers.*`` and register themselves at import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Endpoint:
    """Resolved chat-completions endpoint for one role.

    `extras` carries provider-specific knobs (e.g.
    ``{"chat_template_kwargs": {"enable_thinking": False}}`` for
    mlx-lm, or system-prompt mutators for Anthropic). Caller picks
    what it cares about.
    """

    base_url: str       # full /v1 base, ready for /chat/completions
    api_key: str
    model: str
    provider: str       # registry name, e.g. 'local' / 'gemini' / 'anthropic'
    role: str           # which agent role this was resolved for
    extras: dict        # provider-specific extra body fields


class Provider(Protocol):
    """A provider knows how to build an Endpoint for a given role.

    Implementations live under
    :mod:`aiforge_core.llm.providers`. They register themselves via
    :func:`aiforge_core.llm.providers.register_provider` at import.
    """

    name: str

    def is_available(self) -> bool:
        """True iff this provider can serve a request right now
        (e.g. API key present, host reachable)."""

    def endpoint(self, role: str) -> Endpoint:
        """Build an :class:`Endpoint` for the given agent role."""

    def rate_limits(self) -> dict | None:
        """Declared limits for the rate limiter. Return ``None`` for
        unlimited (e.g. local mlx-lm). Otherwise return a dict with
        any of: ``rpm`` (requests/min), ``tpm`` (tokens/min). Env
        ``AIFORGE_<NAME>_RPM`` / ``_TPM`` overrides at runtime.
        """
