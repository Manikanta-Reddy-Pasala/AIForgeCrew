"""Factory for the smolagents Planner agent.

Model: AIFORGE_PLANNER_MODEL (default qwen3.6-35b-a3b).
Backend: AIFORGE_PLANNER_BACKEND=code|toolcalling (default code).

EVAL-1 (2026-04-23) showed gemma-4-26b-a4b-it never produces a plan and
that qwen3.6-35b-a3b + CodeAgent writes a plan 3/3 runs vs ToolCallingAgent
1/3. Both are kept available; the env flag lets ops fall back instantly.
"""
from __future__ import annotations

import os

from smolagents import CodeAgent, LiteLLMModel, MultiStepAgent, ToolCallingAgent

from .tools import make_tools


PLANNER_PREAMBLE = """You are the Planner. You do NOT write code. Your job is to enrich the
ticket with the information the Doer needs in order to write the code itself:
  - which files to edit
  - a numbered high-level plan
  - verified method signatures (so the Doer doesn't invent method names)
  - known compile pitfalls from memory (so the Doer avoids past mistakes)

Required completion checklist — ALL must hold before you call final_answer:
  [X] lookup_repo called with the ticket's `project` field FIRST. The result gives
      you the authoritative stack, entry command, compile gate, ports, and Dockerfile
      presence from the (:Repo) catalog. Use these values — do NOT invent versions
      from the ticket body. If the ticket says "Java 17" but lookup_repo says
      "Java 24", trust the catalog (the pom.xml is ground truth).
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


def _agent_class(backend: str) -> type[MultiStepAgent]:
    if backend == "toolcalling":
        return ToolCallingAgent
    if backend == "code":
        return CodeAgent
    raise ValueError(
        f"AIFORGE_PLANNER_BACKEND={backend!r}; expected 'code' or 'toolcalling'"
    )


def build_planner_agent(
    ticket: object,
    context_bundle: str,
    llm_config: object,
) -> tuple[MultiStepAgent, str]:
    """Build a smolagents Planner agent for one Planner tick.

    Backend controlled by AIFORGE_PLANNER_BACKEND (default ``code`` — see
    EVAL-1 2026-04-23: CodeAgent wrote the plan in 3/3 runs vs TC 1/3).

    Returns the agent plus the composed task prompt.
    """
    from aiforge_core.runtime.config import WORKTREE_ROOT
    from aiforge_core.runtime.logging_setup import get_logger

    ctx: dict = {
        "ticket": ticket,
        "worktree_root": WORKTREE_ROOT,
        "store": None,
        "log": get_logger("planner"),
    }

    tools = make_tools(ctx)

    import inspect as _inspect_lm
    _lm_params = set(_inspect_lm.signature(LiteLLMModel.__init__).parameters)
    _model_id_key = "model_id" if "model_id" in _lm_params else "model"

    model_id = llm_config.model
    if "/" not in model_id:
        model_id = f"openai/{model_id}"

    # Qwen3.6 ships as a reasoning model; content field stays empty unless we
    # tell LM Studio to disable the thinking trace via chat_template_kwargs.
    model = LiteLLMModel(**{
        _model_id_key: model_id,
        "api_base": llm_config.base_url,
        "api_key": llm_config.api_key,
        "max_tokens": 524288,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    })

    backend = os.environ.get("AIFORGE_PLANNER_BACKEND", "code").strip().lower()
    agent_cls = _agent_class(backend)

    import inspect as _inspect
    _params = set(_inspect.signature(agent_cls.__init__).parameters)
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": 20,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 1
    if agent_cls is CodeAgent and "additional_authorized_imports" in _params:
        _kwargs["additional_authorized_imports"] = ["re", "json"]

    agent = agent_cls(**_kwargs)
    task_prompt = build_task_prompt(ticket, context_bundle)
    return agent, task_prompt
