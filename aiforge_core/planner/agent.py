"""Factory for the smolagents ToolCallingAgent used by the Planner role.

Model hint: AIFORGE_PLANNER_MODEL defaults to gemma-4-26b-a4b-it
(set in scripts/runtime/com.aiforge.graph-runner.plist).
"""
from __future__ import annotations

from smolagents import LiteLLMModel, ToolCallingAgent

from .tools import make_tools


PLANNER_PREAMBLE = """You are the Planner. You do NOT write code. Your job is to enrich the
ticket with the information the Doer needs in order to write the code itself:
  - which files to edit
  - a numbered high-level plan
  - verified method signatures (so the Doer doesn't invent method names)
  - known compile pitfalls from memory (so the Doer avoids past mistakes)

Required completion checklist — ALL must hold before you call final_answer:
  [X] search_memory called at least once to retrieve prior facts + past ticket digests.
  [X] grep_repos called at least once to locate target file(s) in ~/codeRepo.
  [X] read_file called on each candidate to confirm it is the right target.
  [X] extract_signatures called on each target file to pull exact method signatures.
  [X] write_plan called with:
        files:      list of repo-relative paths, each confirmed by read_file
        plan:       numbered high-level steps (not code)
        signatures: the relevant method signatures from extract_signatures, one per line
                    with "path:line: <signature>" prefix
        pitfalls:   compile gotchas pulled from search_memory hits (if any)
        cross_service: only when >1 service is affected
  [X] final_answer with a short summary of what the plan covers.

Hard rules:
- Do NOT write find/replace code blocks. That is the Doer's job. Give signatures and
  pitfalls only — not patches.
- Every file in write_plan.files MUST have been confirmed by a successful read_file.
- If grep_repos returns nothing, widen the glob (try `*.java`, then `**/*.java`) before
  giving up. If still nothing after 3 attempts, call final_answer with exactly:
  'blocked: cannot identify target file from ticket'.
- If the ticket touches more than one service, call create_child_ticket per service and
  summarize the split.
"""


def build_task_prompt(ticket: object, context_bundle: str) -> str:
    """Compose the full task string passed to ``agent.run(task=...)``."""
    body = getattr(ticket, "body", "") or ""
    return (
        f"{PLANNER_PREAMBLE}\n"
        f"## Context bundle\n{context_bundle}\n\n"
        f"## Ticket body\n{body}"
    )


def build_planner_agent(
    ticket: object,
    context_bundle: str,
    llm_config: object,
) -> tuple[ToolCallingAgent, str]:
    """Build a :class:`~smolagents.ToolCallingAgent` for one Planner tick.

    Returns the agent plus the composed task prompt to pass to ``agent.run(task=...)``.

    The ``ctx`` dict is constructed here and injected into every tool factory
    so all tools share the same ticket reference and can mutate it in place.
    """
    import os
    from aiforge_core.runtime.config import WORKTREE_ROOT
    from aiforge_core.runtime.logging_setup import get_logger

    ctx: dict = {
        "ticket": ticket,
        "worktree_root": WORKTREE_ROOT,
        "store": None,  # lazily instantiated inside tools that need it
        "log": get_logger("planner"),
    }

    tools = make_tools(ctx)

    # LiteLLMModel kwarg names differ across smolagents minor versions.
    import inspect as _inspect_lm
    _lm_params = set(_inspect_lm.signature(LiteLLMModel.__init__).parameters)
    _model_id_key = "model_id" if "model_id" in _lm_params else "model"

    # LiteLLM needs a provider prefix for custom OpenAI-compat endpoints (LM Studio).
    model_id = llm_config.model
    if "/" not in model_id:
        model_id = f"openai/{model_id}"

    model = LiteLLMModel(**{
        _model_id_key: model_id,
        "api_base": llm_config.base_url,
        "api_key": llm_config.api_key,
    })

    import inspect as _inspect
    _params = set(_inspect.signature(ToolCallingAgent.__init__).parameters)
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": 20,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 1

    agent = ToolCallingAgent(**_kwargs)
    task_prompt = build_task_prompt(ticket, context_bundle)
    return agent, task_prompt
