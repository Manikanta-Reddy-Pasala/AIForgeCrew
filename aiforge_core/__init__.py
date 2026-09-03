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

# Third-party telemetry off by default, HERE rather than only in run.sh.
# run.sh sets these too, but it is one of several ways this code starts: a test
# run, `python -m aiforge_core.cli`, a bare `uvicorn aiforge_core.api.api:app`
# and any import from another tool all skip it, and those processes were
# phoning home while the operator believed the box was quiet. Same reasoning as
# the cost map above: the import that reads the variable can happen at any
# moment, so the default belongs at the package boundary.
# setdefault, so an operator who genuinely wants telemetry can still export it.
for _var, _off in (
    ("DO_NOT_TRACK", "1"),                    # honoured by a growing set of CLIs
    ("HF_HUB_DISABLE_TELEMETRY", "1"),        # huggingface_hub
    ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1"),   # don't attach a stored HF token
    ("LITELLM_TELEMETRY", "False"),
    ("SCARF_NO_ANALYTICS", "true"),           # scarf-wrapped installers
    ("ANONYMIZED_TELEMETRY", "False"),        # chromadb and friends
    ("POSTHOG_DISABLED", "1"),
    ("TOKENIZERS_PARALLELISM", "false"),      # not telemetry; kills a noisy warn
):
    _os.environ.setdefault(_var, _off)

from .agents import AgentContract, load_agents  # noqa: E402

__all__ = ["AgentContract", "load_agents"]
