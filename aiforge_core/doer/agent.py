"""Factory for the smolagents agent used by the Doer role.

Backend is selected by ``AIFORGE_DOER_BACKEND``:

- ``code`` (default, recommended) → CodeAgent. Matches the Planner's
  backend. Qwen3.6 emits tool calls as Python code which CodeAgent
  executes natively. This was the real fix for ONE-45 — the model kept
  writing ``edit_block(path=..., find=...)`` as a literal string inside
  final_answer because ToolCallingAgent expects structured JSON tool
  calls, not Python syntax.
- ``toolcalling`` → ToolCallingAgent. Works for models that natively
  emit function-calling JSON (GPT-4, Claude). Kept as fallback.
"""
from __future__ import annotations

import os
from pathlib import Path

from smolagents import CodeAgent, LiteLLMModel, MultiStepAgent, ToolCallingAgent

from .scope_guard import ScopeGuard, parse_allowed_files
from .tools import make_tools


# ─────────────────────────── Repo map (Aider) ───────────────────────────
# Source-file extensions Aider's tree-sitter pack actually understands.
# Anything outside this set won't produce ranked tags, so don't bloat
# other_files with binaries / lockfiles / build outputs.
_REPO_MAP_EXTS = {
    ".py", ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".swift", ".scala", ".sh", ".bash", ".zsh", ".sql", ".graphql",
    ".yaml", ".yml", ".toml", ".md",
}
_REPO_MAP_EXCLUDE_DIRS = {
    ".git", ".aider.tags.cache.v4", "node_modules", "target", "build",
    "dist", "__pycache__", ".venv", "venv", ".idea", ".vscode",
    "vendor", "Pods", "DerivedData",
}


def _enumerate_repo_files(root: Path, hard_cap: int = 4000) -> list[str]:
    """Walk *root* and return source files (absolute paths). Stops at
    ``hard_cap`` to keep RepoMap construction bounded."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _REPO_MAP_EXCLUDE_DIRS]
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in _REPO_MAP_EXTS:
                out.append(os.path.join(dirpath, f))
                if len(out) >= hard_cap:
                    return out
    return out


def _build_repo_map_section(worktree_path: str, allowed: set[str]) -> str:
    """Render the Aider RepoMap section for the Doer prompt, or "" when
    disabled / unavailable. Never raises — all errors swallowed inside
    ``render_repo_map``."""
    if os.environ.get("AIFORGE_AIDER_REPOMAP_ENABLED", "").strip() != "1":
        return ""
    try:
        from aiforge_core.index.aider_map import (
            AiderMapConfig,
            render_repo_map_cached as render_repo_map,
        )
    except Exception:
        return ""
    root = Path(worktree_path)
    if not root.is_dir():
        return ""
    # Resolve allowed files to absolute under the worktree where possible.
    chat_files: list[str] = []
    for entry in sorted(allowed):
        cand = entry if os.path.isabs(entry) else str(root / entry)
        if os.path.exists(cand):
            chat_files.append(cand)
    chat_set = set(chat_files)
    other_files = [p for p in _enumerate_repo_files(root) if p not in chat_set]
    map_tokens = int(os.environ.get("AIFORGE_AIDER_REPOMAP_TOKENS", "1024"))
    digest = render_repo_map(
        AiderMapConfig(
            root=root,
            chat_files=chat_files,
            other_files=other_files,
            map_tokens=map_tokens,
        )
    )
    if not digest.strip():
        return ""
    return (
        "\n\n## Code map (top-K ranked by Aider RepoMap)\n"
        f"{digest.rstrip()}\n"
    )


DOER_PREAMBLE = """You are the Doer agent. Your ONLY job is to modify code with edit_block / write_file so the ticket is implemented.

The ticket body from the Planner will typically contain a ## Plan, ## Files, ## Signatures,
and ## Compile pitfalls section. That is context for you. The actual code changes are YOUR
responsibility — you must emit edit_block / write_file calls yourself. There is no auto-apply shortcut.

## When the plan refers to a file you cannot locate

"File not found" is NEVER a terminal state. Do NOT call final_answer with
"blocked: unable to locate". You have three resolution channels — USE THEM
IN THIS ORDER before giving up:

1. **graph_rag MCP** (if available in your tool list):
   - `sym_lookup(name="ProductController")` — find class declarations across
     indexed repos. Returns file:line.
   - `list_endpoints(repo="PosClientBackend")` — enumerates HTTP endpoints.
   - `caller_chain(symbol="...")` / `callee_chain(symbol="...")` — trace
     usage to find the right file.
   - `read_source(path="...", start_line=1, end_line=80)` — read a file by
     its graph path.
   - `find_doc(query="...")` — search CLAUDE.md + docs.

2. **search_memory** (always available): query past tickets + patterns for
   prior resolutions. Ex: search_memory("products batchSize pagination
   PosClientBackend").

3. **Local filesystem (grep + list_dir)** as a last resort. Prefer 1-2
   above because they cost less context.

If after resolution you still can't identify the target, emit ONE
create_child_ticket via orchestrator callback describing what specifically
is missing — do NOT emit final_answer with an empty edit.

IMPORTANT — programmatic enforcement is active:
- The harness tracks edit_block_ok and compile_green counters.
- If you call final_answer without edit_block_ok >= 1 AND compile_green >= 1, the harness rejects your answer and blocks the ticket. The worktree is preserved so the NEXT tick can continue where you left off — so partial progress is not thrown away, but you also don't get credit for a premature final_answer.
- **Tool calls are STRUCTURED, not text.** If you want to call ``edit_block`` you MUST emit it as a tool call in the JSON format your runtime expects — writing the literal string ``edit_block(path="...", find="...", replace="...")`` in your message content does NOTHING. The harness will see edit_block_ok=0 and reject your final_answer. Same for write_file and run_compile.
- **Compile red is NEVER a reason to call final_answer.** If run_compile returns EXIT != 0, you must read the error and make another edit_block immediately — do not stop. You have up to 15 steps in this tick; exhaust them on fixing compile errors before giving up.

Choose the right write tool:
- ``edit_block(path, find, replace)`` — when the file exists and you want to change a narrow slice. Unique find string required.
- ``write_file(path, content)`` — when the file is NEW (does not exist yet) or when you need to overwrite it completely. Creates missing parent directories. Best for docs / analysis tickets that ask you to author a new markdown file.
- ``edit_block(path, find="", replace=CONTENT)`` — equivalent shortcut for creating a new file, if the runtime routes empty-find that way.

Mandatory sequence:
1. read_file on the first file in ## Allowed files to see current shape. If read_file returns "file not found", the target is NEW and you will use write_file in step 3.
2. git_diff_head to see if a previous tick already made edits — if so, your job is to continue from that state (implement what's missing, not re-do what's there).
3. Call the right write tool (edit_block for modifications, write_file for new files). One tool call, narrow scope. A small real edit beats a large planned one.
4. Call run_compile.
5. If EXIT != 0: read the first error message, make ONE targeted fix via another write tool call, call run_compile again. Keep iterating — do NOT call final_answer while compile is red unless you have genuinely exhausted 15 steps trying.
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

**Acceptance gate is active.** Before calling final_answer, the harness scans every file in ## Allowed files and checks that the identifier tokens of each ## Acceptance bullet are present. If any bullet has <50% of its tokens in the file content, final_answer is rejected and you must keep editing. Symptoms of a partial edit (e.g. you added imports but forgot the field declaration, or added the field but forgot the call site) will be caught here. Use a separate edit_block per acceptance bullet — do not bundle multiple bullets into one edit unless they overlap textually.

After every run_compile EXIT=0, run read_file on the target path and explicitly verify each acceptance bullet appears in the new content. Only call final_answer once you can point to the line for every bullet.
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


def _agent_class(backend: str) -> type[MultiStepAgent]:
    """Pick the smolagents agent class for the given backend flag."""
    if backend == "toolcalling":
        return ToolCallingAgent
    if backend == "code":
        return CodeAgent
    raise ValueError(
        f"AIFORGE_DOER_BACKEND={backend!r}; expected 'code' or 'toolcalling'"
    )


def build_doer_agent(
    ticket: object,
    worktree_path: str,
    context_bundle: str,
    llm_config: object,
    counters: dict | None = None,
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> tuple[MultiStepAgent, str]:
    """Build a smolagents MultiStepAgent (Code or ToolCalling) for one Doer tick.

    Backend is selected via ``AIFORGE_DOER_BACKEND`` (default: ``code``).

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
    # agent_config resolves provider+model+base_url per role from the
    # persisted JSON (UI Settings page) or env override. Legacy callers
    # that pass llm_config directly are honored by falling back.
    try:
        from aiforge_core.runtime import agent_config as _acfg
        k = _acfg.resolve_litellm("doer")
        model_id = k["model_id"]
        api_base = k["api_base"]
        api_key = k["api_key"]
    except Exception:
        model_id = llm_config.model
        if "/" not in model_id:
            model_id = f"openai/{model_id}"
        api_base = llm_config.base_url
        api_key = llm_config.api_key
    # Lower temperature = more deterministic tool-call emission. Coder
    # models drift into "thinking out loud" content at default 0.7,
    # which burns steps without firing edit_block. AIFORGE_DOER_TEMP
    # overrides per run.
    _temp = float(os.environ.get("AIFORGE_DOER_TEMP", "0.2"))
    model = LiteLLMModel(**{
        _model_id_key: model_id,
        "api_base": api_base,
        "api_key": api_key,
        "max_tokens": 524288,
        "temperature": _temp,
        # Harmless on non-reasoning models; required on Qwen3.6 family so
        # message.content actually gets populated instead of reasoning_content.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    })
    from aiforge_core.runtime.llm_trace import attach_trace
    from aiforge_core.runtime.logging_setup import get_logger
    attach_trace(
        model, get_logger("doer"), role="doer",
        ticket=getattr(ticket, "identifier", None),
    )

    backend = os.environ.get("AIFORGE_DOER_BACKEND", "code").lower()
    AgentCls = _agent_class(backend)

    # smolagents agents 1.24 have no `system_prompt` kwarg; full context is
    # delivered via the task string passed to .run().
    import inspect as _inspect
    _params = set(_inspect.signature(AgentCls.__init__).parameters)
    # max_steps 20: analysis/docs tickets can need 4-6 exploratory reads
    # before the first write; 12 wasn't enough and CodeAgent spent all
    # steps on grep/read then called final_answer without writing
    # (observed on ONE-45 cross-repo analysis). AIFORGE_DOER_MAX_STEPS
    # overrides per run.
    max_steps = int(os.environ.get("AIFORGE_DOER_MAX_STEPS", "20"))
    _kwargs: dict = {
        "tools": tools,
        "model": model,
        "max_steps": max_steps,
    }
    if "num_retries" in _params:
        _kwargs["num_retries"] = 2
    # planning_interval forces the agent to pause and replan every N
    # steps. Each replan emits a 10K-char "facts survey + plan" assistant
    # turn that bloats ctx fast (ONE-51 step 19: 6 replans = ~60KB on a
    # single step). Scale by ticket complexity:
    #   * single allowed-files entry → planning_interval=None (no replan)
    #   * 2-3 entries                 → 4 (default)
    #   * 4+ entries                  → 2 (multi-file, replan often)
    # AIFORGE_DOER_PLANNING_INTERVAL overrides per run (use ``none`` to
    # disable explicitly).
    if "planning_interval" in _params:
        env_pi = os.environ.get("AIFORGE_DOER_PLANNING_INTERVAL", "").strip().lower()
        if env_pi == "none":
            pi = None
        elif env_pi.isdigit():
            pi = int(env_pi)
        else:
            n_files = len(allowed)
            pi = None if n_files <= 1 else (4 if n_files <= 3 else 2)
        if pi is not None:
            _kwargs["planning_interval"] = pi
    # Acceptance gate — final_answer is rejected unless every bullet from
    # the ticket's ## Acceptance section has its identifier tokens present
    # in the allowed-files blob. Forces the model to actually implement
    # each item, not just compile-green a partial edit.
    if "final_answer_checks" in _params:
        from .acceptance_gate import make_acceptance_check
        _check = make_acceptance_check(body, worktree_path, allowed)
        if _check is not None:
            _kwargs["final_answer_checks"] = [_check]
    # CodeAgent-specific: restrict Python imports so the model can't sidestep
    # the scope_guard by calling open() / subprocess directly. Our @tool
    # functions already cover every FS op it should need.
    if AgentCls is CodeAgent and "additional_authorized_imports" in _params:
        _kwargs["additional_authorized_imports"] = [
            "pathlib", "re", "json", "textwrap",
        ]
    agent = AgentCls(**_kwargs)
    repo_map_section = _build_repo_map_section(worktree_path, allowed)
    enriched_bundle = (context_bundle or "") + repo_map_section
    task_prompt = build_task_prompt(
        ticket, enriched_bundle, allowed,
        prior_verdict=prior_verdict, prior_fixlist=prior_fixlist,
    )
    return agent, task_prompt
