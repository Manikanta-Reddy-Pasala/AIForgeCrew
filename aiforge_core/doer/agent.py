"""Factory for the smolagents ToolCallingAgent used by the Doer role."""
from __future__ import annotations

from smolagents import LiteLLMModel, ToolCallingAgent

from .scope_guard import ScopeGuard, parse_allowed_files
from .tools import make_tools


DOER_PREAMBLE = """You are the Doer agent. Your ONLY job is to modify code with edit_block so the ticket is implemented.

The ticket body from the Planner will typically contain a ## Plan, ## Files, ## Signatures,
and ## Compile pitfalls section. That is context for you. The actual code changes are YOUR
responsibility — you must emit edit_block calls yourself. There is no auto-apply shortcut.

IMPORTANT — programmatic enforcement is active:
- The harness tracks edit_block_ok and compile_green counters.
- If you call final_answer without edit_block_ok >= 1 AND compile_green >= 1, the harness rejects your answer and blocks the ticket. The worktree is preserved so the NEXT tick can continue where you left off — so partial progress is not thrown away, but you also don't get credit for a premature final_answer.
- **Compile red is NEVER a reason to call final_answer.** If run_compile returns EXIT != 0, you must read the error and make another edit_block immediately — do not stop. You have up to 15 steps in this tick; exhaust them on fixing compile errors before giving up.

Mandatory sequence:
1. read_file on the first file in ## Allowed files to see current shape.
2. git_diff_head to see if a previous tick already made edits — if so, your job is to continue from that state (implement what's missing, not re-do what's there).
3. Call edit_block with a narrow find/replace block that implements the NEXT missing piece from the ticket plan. A small real edit beats a large planned one.
4. Call run_compile.
5. If EXIT != 0: read the first error message, make ONE targeted fix via another edit_block, call run_compile again. Keep iterating — do NOT call final_answer while compile is red unless you have genuinely exhausted 15 steps trying.
6. When run_compile returns EXIT=0, verify you have implemented every bullet in the previous feedback's fix list (if present in ## Previous feedback). If the list is covered AND compile is green, call final_answer with a one-paragraph summary citing the change + the EXIT=0 evidence.

Minimal example of step 2 for a Spring @RequestMapping with limit/offset pagination:
```
edit_block(
  path="src/main/java/.../ClientRequestController.java",
  find="public Mono<ResponseEntity<?>> queryAndProcess(@RequestBody MessageRequest<T> request) {",
  replace="public Mono<ResponseEntity<?>> queryAndProcess(@RequestBody MessageRequest<T> request, @RequestParam(defaultValue = \"5000\") int limit, @RequestParam(defaultValue = \"0\") int offset) {"
)
```
That's it — small, concrete, one call. Then compile, then fix anything that breaks.

Hard rules:
- ONLY edit files listed in the ## Allowed files section. Scope violations abort.
- Do NOT invent method names. If grep returns nothing for a method you want to call, it doesn't exist.
- Do NOT rewrite the entire file in one edit_block — keep find/replace narrow.
- When using a Java annotation (e.g. @RequestParam, @RequestBody), make sure the matching `import org.springframework...` line exists at the top of the file.
- When a .map() lambda has conditional branches each returning ResponseEntity.ok(...), cast each branch to (ResponseEntity<?>) to satisfy type inference.
- If compile is still red after you have genuinely tried 5+ distinct edit_block fixes (not the same one in a loop), THEN and only then call final_answer with "blocked: compile red after N attempts — " followed by the specific error.

DO NOT just read and claim done. The only acceptable path to final_answer (without "blocked:") is: edit_block → compile green → all feedback-fixlist bullets addressed → final_answer.
"""


def build_task_prompt(
    ticket: object,
    context_bundle: str,
    allowed: set[str],
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> str:
    scope_block = (
        "\n\n## Allowed files (scope guard — write-tool violations are blocked)\n"
        + (
            "\n".join(f"- {p}" for p in sorted(allowed))
            if allowed
            else "(no ## Files section — writes unrestricted by scope guard)"
        )
    )
    feedback_block = ""
    if prior_verdict == "fail" and (prior_fixlist or "").strip():
        feedback_block = (
            "\n\n## Previous feedback (fix list — address every bullet before final_answer)\n"
            f"{prior_fixlist.strip()}"
        )
    body = getattr(ticket, "body", "") or ""
    return (
        f"{DOER_PREAMBLE}\n"
        f"## Context bundle\n{context_bundle}"
        f"{scope_block}"
        f"{feedback_block}\n\n"
        f"## Ticket body\n{body}"
    )


def build_doer_agent(
    ticket: object,
    worktree_path: str,
    context_bundle: str,
    llm_config: object,
    counters: dict | None = None,
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> tuple[ToolCallingAgent, str]:
    """Build a :class:`~smolagents.ToolCallingAgent` for one Doer tick.

    If *counters* is provided, edit_block and run_compile will bump it so
    the caller can verify real work happened before accepting final_answer.

    Returns the agent plus the composed task prompt to pass to ``agent.run(task=...)``.
    """
    if counters is None:
        counters = {}
    allowed = parse_allowed_files(getattr(ticket, "body", "") or "")
    scope_guard = ScopeGuard(allowed)
    # Pass the ticket body as a provider so the apply_implementation tool can
    # parse the latest planner-written ## Implementation section.
    body = getattr(ticket, "body", "") or ""
    tools = make_tools(worktree_path, scope_guard, counters,
                       ticket_body_provider=lambda: body)

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
        # Harmless on non-reasoning models (Qwen3-Coder), required on Qwen3.6
        # family so message.content actually gets populated.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    })

    # smolagents ToolCallingAgent 1.24 has no `system_prompt` kwarg; full
    # context is delivered via the task string passed to .run().
    import inspect as _inspect
    _params = set(_inspect.signature(ToolCallingAgent.__init__).parameters)
    # max_steps 12: tight enough to avoid qwen3-coder-next's tool-call grammar
    # drift on long multi-turn runs (ONE-16 tick 2: agent emitted raw
    # <tool_call> text as prose at step ~14). Enough room to do 3 edits +
    # 3 compile attempts + final_answer.
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": 12,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 2
    # planning_interval forces the agent to pause and replan every N steps,
    # which helps qwen-coder avoid the read→compile→final_answer shortcut.
    if "planning_interval" in _params:
        _kwargs["planning_interval"] = 4
    agent = ToolCallingAgent(**_kwargs)
    task_prompt = build_task_prompt(
        ticket, context_bundle, allowed,
        prior_verdict=prior_verdict, prior_fixlist=prior_fixlist,
    )
    return agent, task_prompt
