"""Factory for the smolagents ToolCallingAgent used by the Doer role."""
from __future__ import annotations

from smolagents import LiteLLMModel, ToolCallingAgent

from .scope_guard import ScopeGuard, parse_allowed_files
from .tools import make_tools


DOER_PREAMBLE = """You are the Doer agent. You MUST implement the ticket by modifying code. Reading files alone is not acceptable.

Required completion checklist — all three must hold before you call final_answer:
  [X] At least one successful edit_block call (tool returned "OK: edited ...").
  [X] At least one run_compile call whose result is exactly "EXIT=0" (compile green).
  [X] Your final_answer summary references the specific change you made and cites the compile-green EXIT=0 evidence.

Strategy:
1. read_file the target file to see the current shape.
2. grep or read_file any referenced service to verify method signatures before calling them.
3. Plan the minimal edit.
4. Call edit_block with a narrow find/replace block. Do NOT put the whole file in `find`.
5. Call run_compile. If EXIT != 0: read the first error, make ONE targeted fix via another edit_block, retry. Max 3 compile attempts total.
6. Only when EXIT=0 has been observed from run_compile, call final_answer with a summary.

Hard rules:
- Never call final_answer before successful edit_block + run_compile EXIT=0.
- ONLY edit files listed in the ## Allowed files section. Scope violations abort.
- Do NOT invent method names. If grep returns nothing for a method you want to call, it doesn't exist.
- Do NOT rewrite the entire file in one edit_block — keep find/replace narrow.
- When using a Java annotation (e.g. @RequestParam, @RequestBody), verify the matching `import org.springframework...` line exists at the top of the file. If it's missing, add it with edit_block before calling run_compile. "cannot find symbol: class RequestParam" means this import is absent.
- If after 3 failed compile attempts compile is still red, call final_answer with "blocked: compile red after 3 attempts — " followed by the specific error. This will mark the ticket blocked for human review.
"""


def build_task_prompt(ticket: object, context_bundle: str, allowed: set[str]) -> str:
    scope_block = (
        "\n\n## Allowed files (scope guard — write-tool violations are blocked)\n"
        + (
            "\n".join(f"- {p}" for p in sorted(allowed))
            if allowed
            else "(no ## Files section — writes unrestricted by scope guard)"
        )
    )
    body = getattr(ticket, "body", "") or ""
    return (
        f"{DOER_PREAMBLE}\n"
        f"## Context bundle\n{context_bundle}"
        f"{scope_block}\n\n"
        f"## Ticket body\n{body}"
    )


def build_doer_agent(
    ticket: object,
    worktree_path: str,
    context_bundle: str,
    llm_config: object,
) -> tuple[ToolCallingAgent, str]:
    """Build a :class:`~smolagents.ToolCallingAgent` for one Doer tick.

    Returns the agent plus the composed task prompt to pass to ``agent.run(task=...)``.
    """
    allowed = parse_allowed_files(getattr(ticket, "body", "") or "")
    scope_guard = ScopeGuard(allowed)
    tools = make_tools(worktree_path, scope_guard)

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

    # smolagents ToolCallingAgent 1.24 has no `system_prompt` kwarg; full
    # context is delivered via the task string passed to .run().
    import inspect as _inspect
    _params = set(_inspect.signature(ToolCallingAgent.__init__).parameters)
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": 15,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 1
    agent = ToolCallingAgent(**_kwargs)
    task_prompt = build_task_prompt(ticket, context_bundle, allowed)
    return agent, task_prompt
