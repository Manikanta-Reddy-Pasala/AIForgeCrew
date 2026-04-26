"""Pluggable LLM layer — one provider registry, one complete() entry.

Public surface:

* :func:`complete(role, messages, **kwargs)` — issue a chat call for
  the given role. Routes via :mod:`router` to the right provider,
  retries to the fallback provider on transport errors, returns the
  assistant's text content.
* :func:`resolve(role) -> Endpoint` — what would `complete` use?
  Inspect without making the call.
* :data:`PROVIDERS` — registry mapping ``name → Provider`` instance.
  Add new provider:

      from aiforge_core.llm.providers import register_provider
      from aiforge_core.llm.types import Provider, Endpoint
      class MyProvider(Provider): ...
      register_provider(MyProvider())

Per-role override: set ``AIFORGE_<ROLE>_PROVIDER=<name>``. Falls back
to ``AIFORGE_PRIMARY_BACKEND`` (legacy: ``AIFORGE_DOER_PRIMARY_BACKEND``)
then to ``"local"``.
"""
from .types import Endpoint, Provider
from .router import resolve, fallback, list_providers
from .client import complete
from .rate_limiter import acquire as rl_acquire, state as rl_state

__all__ = [
    "Endpoint", "Provider",
    "resolve", "fallback", "list_providers",
    "complete",
    "rl_acquire", "rl_state",
]
