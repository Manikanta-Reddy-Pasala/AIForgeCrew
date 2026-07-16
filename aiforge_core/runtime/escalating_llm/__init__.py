"""Self-healing ADK ``BaseLlm`` wrapper with cloud escalation.

Wraps a primary ADK model (LiteLlm against the operator's local mlx-lm)
and an ordered list of cloud fallbacks
(Ollama Cloud). On failure of the
primary the wrapper transparently retries the same request against
each cloud entry in turn, so the agent loop never stalls on a flaky
local model.

Failure triggers (any of):

* primary raises any exception during ``generate_content_async``
* primary yields zero responses (mlx-lm tool_calls bug surface)
* primary's final response carries no text and no tool calls
  (model lost the plot — usually a hallucinated stop token)

The wrapper is intentionally non-streaming: ADK ``LlmAgent`` /
``LoopAgent`` request ``stream=False`` by default for the v6 pipeline.
If the caller asks for streaming we honour the primary directly without
the retry chain — partial-chunk re-emission across providers would
violate the streaming contract.

This module was split (grouped by concern) into ``_quieting`` /
``_policy`` / ``_builder`` / ``_wrapper`` submodules; this package
re-exports the full former top-level surface so every
``escalating_llm.<name>`` attribute access is unchanged.
"""
from __future__ import annotations

from ._quieting import (
    _StripTracebackFilter,
    _install_adk_toolarg_repair,
    _quiet_adk_tracebacks,
    _quiet_litellm,
    _repair_json,
    _truthy_env,
    log,
)
from ._policy import (
    _TRANSIENT_MARKERS,
    _api_base_of,
    _attempt_retries,
    _demote_after,
    _is_empty,
    _is_transient_llm_error,
)
from ._builder import _build_one, _mirror_to_langfuse
from ._wrapper import EscalatingLlm

__all__ = ["EscalatingLlm"]


# Apply at import — before ANY litellm call / team run — so the worker is
# never even created (not just lazily when the first LiteLlm is built).
_quiet_litellm()
_quiet_adk_tracebacks()
