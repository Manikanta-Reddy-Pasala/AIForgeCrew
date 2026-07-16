"""Per-archetype model + provider config, persisted to a JSON file.

The 6 archetypes match the v5 production pipeline (see
``aiforge_core/agents/agents.yaml`` + ``runtime.adk_runner``):

    architect, planner, verifier, doer, feedback, learner

Architect is external (human-driven operator session) but still
configurable here for trace symmetry — its model pin is read by the
operator's external client. The other five run inside the ADK SequentialAgent:
``Planner → Verifier → LoopAgent[Doer, Feedback] → Learner``.

Each archetype can be flipped between providers (local mlx_lm / Ollama
Cloud / any OpenAI-compatible endpoint) without a redeploy. Env vars still
override at read time so ops keeps a final-say escape hatch.

Storage: ``$AIFORGE_CONFIG_DIR/agent_config.json`` (default ``~/.aiforge``).

This module was split (grouped by concern) into ``_state`` / ``_resolve`` /
``_persist`` / ``_litellm`` / ``_tools`` / ``_public`` submodules; this package
re-exports the full former public surface so
``from aiforge_core.config import agent_config`` and every
``agent_config.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._litellm import (
    KNOWN_PREFIXES,
    _CLOUD_PROVIDERS_ORDERED,
    cloud_default_for_local,
    cloud_escalation_chain,
    resolve_litellm,
)
from ._persist import _LOCK, reset, set_role
from ._public import (
    PROFILES,
    apply_profile,
    archetypes,
    list_models,
    list_providers,
    profiles,
)
from ._resolve import (
    _CHEAP_ROLES,
    _defaults,
    _global_default_row,
    _row_for,
    cheap_model_for,
    get,
    load_all,
)
from ._state import (
    _ARCHETYPES,
    _CATALOG_CACHE,
    _CATALOG_LOCK,
    _CATALOG_TTL_S,
    _DEFAULT_KEY,
    _FALLBACK_WARNED,
    _LOCAL_DEFAULT_CACHE,
    _LOCAL_DEFAULT_TTL_S,
    _LOCAL_FALLBACK_MODEL,
    _ROLES,
    _enriched_catalog,
    _fc,
    _local_default_model,
    _path,
    MODEL_CATALOG,
    PROVIDERS,
)
from ._tools import _AGENTS_CONTRACTS_CACHE, _agent_contracts, allowed_tools_for

__all__ = [
    "PROVIDERS", "KNOWN_PREFIXES", "MODEL_CATALOG",
    "load_all", "get", "cheap_model_for", "set_role", "reset",
    "resolve_litellm", "cloud_escalation_chain", "cloud_default_for_local",
    "allowed_tools_for", "archetypes", "list_providers", "list_models",
    "PROFILES", "profiles", "apply_profile",
]
