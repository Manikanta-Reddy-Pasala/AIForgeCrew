"""Factory for the smolagents ToolCallingAgent used by the Planner role.

Model hint: AIFORGE_PLANNER_MODEL defaults to gemma-4-26b-a4b-it
(set in scripts/runtime/com.aiforge.graph-runner.plist).
"""
from __future__ import annotations

from smolagents import LiteLLMModel, ToolCallingAgent

from .tools import make_tools


PLANNER_PREAMBLE = """You are the Planner agent.  Your sole job is to enrich the ticket body with
concrete file targets, a numbered implementation plan, and verbatim find/replace blocks
before the Doer picks it up.

Required completion checklist — ALL must hold before you call final_answer:
  [X] You have called search_memory at least once to retrieve relevant prior facts.
  [X] You have called grep_repos at least once to find the target file(s) in the codebase.
  [X] You have called read_file on at least one candidate file to verify its content.
  [X] You have called extract_signatures on each file to get exact method signatures.
  [X] You have called write_plan with a real files list, a concrete numbered plan, and a
      non-empty implementation block containing verbatim find/replace pairs.
  [X] Your final_answer summary describes which files were identified and what the plan says.

Mandatory sequence:
1. Use search_memory to find past tickets solving similar problems (query the ticket title).
2. Use grep_repos to locate endpoint definitions or class names mentioned in the ticket.
3. For each candidate file, call read_file to verify it is the right target, then call
   extract_signatures to get the method signature(s) you will be modifying.
4. For each modification, produce a concrete find/replace block you would put in a ToolCall,
   using the EXACT method signature from extract_signatures as the start of the ``find``
   string.  Do NOT paraphrase or abbreviate.
5. Call write_plan with:
     - files: list of repo-relative paths
     - plan: one-paragraph summary of the approach
     - implementation: a markdown block with one ``### <path>`` heading per file, then one
       or more fenced blocks of the form:
         ```
         find:
         <exact current text, verbatim>
         ---
         replace:
         <new text>
         ```
     - cross_service: (optional) only when the ticket spans 2+ services
6. If the ticket touches more than one service, call create_child_ticket per service and
   summarize the split.
7. Call final_answer when done.

Hard rules:
- Do NOT invent file paths.  Every path in write_plan.files MUST have been confirmed
  by a read_file call that returned actual file content (not an ERROR).
- The ``find`` text in each implementation block MUST be a verbatim substring of the
  current file (use read_file to confirm).  Do NOT paraphrase or abbreviate.
- If grep_repos returns no matches, try a broader glob or a simpler pattern before giving up.
- If you still cannot identify the target file(s) with certainty after 3 grep attempts,
  call final_answer with exactly: 'blocked: cannot identify target file from ticket'
- Never call write_plan with an empty files list unless you are also calling final_answer
  with the blocked message.
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
