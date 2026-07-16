"""Shared state, constants, provider catalog, and leaf helpers for
``agent_config`` (split submodule — re-exported by the package ``__init__``).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from aiforge_core.config import _filecache as _fc

# v6 production pipeline — what the live ADK SequentialAgent runs.
# Order matches execution: architect (external) → triage → planner →
# verifier → researcher → LoopAgent[doer, refiner, feedback] → learner.
_ARCHETYPES: tuple[str, ...] = (
    "architect", "planner", "verifier", "doer", "feedback", "learner",
    # Extended pipeline (2026-05-07).
    "triage", "researcher", "refiner",
    # Post-validator live boot (2026-05-23): runs the recipe under
    # aiforge_core/recipes/live_verify/<project>.md.
    "live_verifier",
    # Parallel context-gathering fan-out (ParallelAgent) — see
    # runtime.parallel_stages. Read-only gatherers, local-tier by default.
    "ctx_memory", "ctx_repomap", "ctx_conventions",
    # Parallel verifier fan-out (ParallelAgent) — three axis critics
    # merged into verifier_verdict.
    "verify_correctness", "verify_scope", "verify_risk",
    # Research-completeness critic (2026-06-18) — drives the bounded
    # research-gap re-search loop. Local-tier, tool-less.
    "gap_eval",
    # Conversational chat agent (2026-06-21) — the UI chat's own model
    # slot, configured on the home page; independent of the pipeline.
    "chat",
    # Parallel orchestrator layer-1 agents (2026-06-26): enhancer (analyze →
    # spec) and architect (file/structure plan) → planner splits. Configurable
    # so the splitter can run on a stronger reasoning model than the workers.
    "enhancer",
)
_ROLES = _ARCHETYPES

# Local default — resolved dynamically, in order:
#   1. AIFORGE_LOCAL_DEFAULT_MODEL env (operator pin)
#   2. first model id served by the local /v1/models endpoint (5-min cache)
#   3. neutral placeholder (only when the server is unreachable AND no env
#      pin AND nothing configured) — a generic id, NOT a hardcoded absolute
#      model path from one operator's laptop (that phantom path was confusing
#      on every other host and produced "Connection error" against a model
#      nobody configured).
_LOCAL_FALLBACK_MODEL = "local-model-unconfigured"
_LOCAL_DEFAULT_CACHE: list[Any] = [0.0, None]  # [ts, model_id]
_LOCAL_DEFAULT_TTL_S = 300.0
_FALLBACK_WARNED = [False]


def _local_default_model() -> str:
    """Resolve a fallback model id when none is configured.

    ``openai_compatible`` is the only provider now; its model list comes
    from the per-role ``/v1/models`` probe (UI-driven), so there is no
    discovery here. Returns the operator's env pin, else a neutral
    placeholder (with a one-time warning) when nothing is configured.
    """
    env = os.environ.get("AIFORGE_LOCAL_DEFAULT_MODEL")
    if env and env.strip():
        return env.strip()
    # Nothing configured and env unset. Don't fabricate a real-looking model
    # — return a neutral placeholder and tell the operator once how to fix it.
    if not _FALLBACK_WARNED[0]:
        _FALLBACK_WARNED[0] = True
        logging.getLogger("aiforge.agent_config").warning(
            "no model configured — using placeholder '%s' (every call will "
            "fail). Fix: set a model in the UI (Home), pin "
            "AIFORGE_LOCAL_DEFAULT_MODEL, or point a role's base_url at a live "
            "OpenAI-compatible endpoint.",
            _LOCAL_FALLBACK_MODEL)
    return _LOCAL_FALLBACK_MODEL

PROVIDERS: dict[str, dict[str, Any]] = {
    # Generic OpenAI-compatible endpoint — the only provider. User supplies
    # base_url (+ optional api_key) per role via the home page. Covers
    # OSS-no-token, LM Studio, OpenRouter, Groq, Together, vLLM, and
    # cloud-with-key. Blank key = no token.
    "openai_compatible": {
        "label": "OpenAI-compatible (any base URL — OSS / LM Studio / OpenRouter / cloud)",
        "litellm_prefix": "openai",
        "default_model": None,          # free-text / discovered via /v1/models
        "api_key_env": "AIFORGE_OPENAI_COMPAT_API_KEY",
        "api_key_default": "not-needed",  # OSS endpoints ignore the key
        "base_url": None,               # required per-role (UI-set)
    },
}

_DEFAULT_KEY = "_default"


# ────────────────────────── Model catalog ──────────────────────────────
# openai_compatible is fully dynamic — the model list depends on the
# per-role base_url, so the UI fetches it on demand via the provider-probe
# endpoint (/v1/models) rather than from this static catalog.

MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "openai_compatible": [],
}

# Module-level cache retained for the reset() cache-clear contract (and
# tests) — no longer populated now that catalog discovery is UI-driven.
_CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CATALOG_TTL_S = 300.0
_CATALOG_LOCK = threading.Lock()


def _enriched_catalog(provider: str) -> list[dict[str, Any]]:
    """Static curated list for a provider.

    ``openai_compatible`` carries no static catalog — its model list is
    discovered per-role from the configured endpoint's ``/v1/models`` via
    the UI provider-probe, not from here.
    """
    return list(MODEL_CATALOG.get(provider) or [])


def _path() -> Path:
    root = Path(os.environ.get("AIFORGE_CONFIG_DIR",
                               os.path.expanduser("~/.aiforge")))
    root.mkdir(parents=True, exist_ok=True)
    return root / "agent_config.json"
