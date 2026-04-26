"""Backwards-compat shim — delegates to the pluggable :mod:`aiforge_core.llm`.

Older callers do ``from aiforge_core.runtime.llm_picker import pick``;
they keep working, but new code should import from
:mod:`aiforge_core.llm` directly:

    from aiforge_core.llm import resolve, fallback, complete

Removed in a later cycle once all sites are migrated.
"""
from __future__ import annotations

from aiforge_core.llm import Endpoint as _Endpoint
from aiforge_core.llm import resolve as _resolve
from aiforge_core.llm import fallback as _fallback


# Older NamedTuple shape mapped onto the new Endpoint dataclass — same
# attribute names so existing destructuring (ep.base_url, ep.backend,
# ep.api_key, ep.model) still works. Adds ``role`` and ``extras``.
class LLMEndpoint(_Endpoint):  # type: ignore[misc]
    @property
    def backend(self) -> str:  # legacy alias for Endpoint.provider
        return self.provider


def pick(role: str) -> LLMEndpoint:
    ep = _resolve(role)
    return LLMEndpoint(
        base_url=ep.base_url, api_key=ep.api_key, model=ep.model,
        provider=ep.provider, role=ep.role, extras=ep.extras,
    )


def fallback(role: str) -> LLMEndpoint | None:
    ep = _fallback(role)
    if ep is None:
        return None
    return LLMEndpoint(
        base_url=ep.base_url, api_key=ep.api_key, model=ep.model,
        provider=ep.provider, role=ep.role, extras=ep.extras,
    )
