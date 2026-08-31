"""AIForgeCrew runtime package (v5).

Public API surface re-exported here intentionally narrow — the canonical
agent contracts loaded from ``agents.yaml``. Subpackages
(``doer``, ``planner``, ``index``, ``memory``, ``eval``, ``runtime``) are
imported directly by callers; legacy v4 code lives in ``aiforge_core.memory``
pending Phase 11 removal.
"""
from __future__ import annotations

import os as _os

# litellm reads this at ITS OWN import time, and it is imported lazily from
# several call sites — so the default has to be in place before any of them
# run, which makes this package's __init__ the only reliable choke point.
# Without it every process start does a network round-trip to
# raw.githubusercontent.com for the model cost map and logs a warning when it
# fails; the fallback is the backup map bundled in the litellm wheel, so on a
# local-model box the fetch cannot produce a better answer than skipping it.
# setdefault, not assignment: run.sh, compose and the operator can still ask
# for the remote map by exporting LITELLM_LOCAL_MODEL_COST_MAP=False.
_os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from .agents import AgentContract, load_agents  # noqa: E402

__all__ = ["AgentContract", "load_agents"]
