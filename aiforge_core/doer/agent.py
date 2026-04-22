"""Factory for the smolagents ToolCallingAgent used by the Doer role."""
from __future__ import annotations

from smolagents import LiteLLMModel, ToolCallingAgent

from aiforge_core.runtime.roles import DOER_SYSTEM

from .scope_guard import ScopeGuard, parse_allowed_files
from .tools import make_tools


def build_doer_agent(
    ticket: object,
    worktree_path: str,
    context_bundle: str,
    llm_config: object,
) -> ToolCallingAgent:
    """Build a :class:`~smolagents.ToolCallingAgent` for one Doer tick.

    Args:
        ticket: Ticket dataclass with ``.body`` and ``.identifier`` fields.
        worktree_path: Absolute path to the git worktree for the ticket.
        context_bundle: Pre-built context string (deep-context + linked tickets).
        llm_config: Any object exposing ``.base_url``, ``.model``, ``.api_key``.
    """
    allowed = parse_allowed_files(getattr(ticket, "body", "") or "")
    scope_guard = ScopeGuard(allowed)
    tools = make_tools(worktree_path, scope_guard)

    # LiteLLMModel kwarg names differ across smolagents minor versions.
    import inspect as _inspect_lm
    _lm_params = set(_inspect_lm.signature(LiteLLMModel.__init__).parameters)
    _model_id_key = "model_id" if "model_id" in _lm_params else "model"
    model = LiteLLMModel(**{
        _model_id_key: llm_config.model,
        "api_base": llm_config.base_url,
        "api_key": llm_config.api_key,
    })

    # Combine DOER_SYSTEM prompt with context bundle and explicit scope block.
    scope_block = (
        "\n\n## Allowed files (scope guard — write-tool violations are blocked)\n"
        + (
            "\n".join(f"- {p}" for p in sorted(allowed))
            if allowed
            else "(no ## Files section — writes unrestricted by scope guard)"
        )
    )
    system_prompt = (
        f"{DOER_SYSTEM}\n\n"
        f"## Context bundle\n{context_bundle}"
        f"{scope_block}"
    )

    # num_retries was added in smolagents 1.14; guard for 1.13 compatibility.
    import inspect as _inspect
    _params = set(_inspect.signature(ToolCallingAgent.__init__).parameters)
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": 15,
        "system_prompt": system_prompt,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 1
    return ToolCallingAgent(**_kwargs)
