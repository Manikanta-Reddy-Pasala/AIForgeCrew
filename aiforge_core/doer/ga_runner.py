"""GenericAgent text-protocol adapter for the Doer role.

Runs the Doer's edit/compile loop through GenericAgent's
``agent_runner_loop`` instead of smolagents.  Used when
``AIFORGE_DOER_BACKEND=genericagent`` (or when ``agents.yaml`` declares
``backend: genericagent_text_protocol``).

Why this exists
---------------
mlx_lm 0.31 emits ``finish_reason=tool_calls`` for Qwen3-Coder-Next but
drops ``message.tool_calls`` from the response payload, so smolagents'
ToolCallingAgent never sees the calls.  GenericAgent works around this
with a *text-protocol* session (the ``LLMSession`` class — note: NOT
``NativeOAISession`` — which detects model intent from inline-formatted
tool calls inside ``message.content`` and re-builds ``tool_calls`` there).
``oai_doer_config`` in ``mykey.py`` deliberately omits ``native_`` from
its name to force GenericAgent to use the text-protocol path.

Design notes
------------
* AIForge concerns we *must* preserve: ScopeGuard write-allowlist,
  acceptance_gate (final-answer rejection if acceptance bullets aren't
  in the file content), counter-tracking for edit_block_ok / compile_green.
* AIForge concerns we *don't* preserve here (out of scope for the bridge):
  smolagents repo-map injection (Aider tags), per-step planning_interval,
  smolagents-specific final_answer_checks.  The GenericAgent prompt
  already pushes the model toward "edit→compile→repeat" and the
  acceptance bullets land via the ticket body itself.
* Tools we explicitly *forbid* from GA's default tools_schema:
  ``ask_user`` (Doer must not block on user input) and
  ``start_long_term_update`` (Doer cannot write memory — that's the
  Learner's job).  Filtering happens at schema level so the model
  never sees them as options, plus a hard rejection in
  ``tool_before_callback`` as a belt-and-braces guard.

The result shape is the same dict produced by ``run_smolagents_doer`` so
``aiforge_core.graph.nodes.doer`` and the orchestrator don't have to
fork.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import emit

from .scope_guard import ScopeGuard, ScopeViolation, parse_allowed_files
from .ga_compat import (
    GA_COMPAT_VERSION, ParentShim, ga_dir, ga_sha,
    import_ga, load_tools_schema,
)
from aiforge_core.memory.neo4j_facts import (
    set_fetch_context, clear_fetch_context, fetch_facts_text,
)
from aiforge_core.memory.code_context import (
    aider_digest, graph_neighbours,
)


def _ga_dir() -> str:
    """Backwards-compat wrapper. Use ga_compat.ga_dir() in new code."""
    return ga_dir()


# Tools the Doer is forbidden from calling per agents.yaml. Filtered out
# of the schema and rejected by tool_before_callback as a second layer.
_FORBIDDEN_GA_TOOLS = {
    "ask_user",
    "start_long_term_update",
    "web_scan",
    "web_execute_js",
    # update_working_checkpoint is handy on long sessions but adds ~900
    # chars of tool description to every turn. Drop for the doer — its
    # work is short-horizon (read → patch → compile → done). Re-enable
    # via AIFORGE_DOER_KEEP_CHECKPOINT=1 if a long-running fixture needs
    # the scratchpad.
    *(set() if os.environ.get("AIFORGE_DOER_KEEP_CHECKPOINT") == "1"
      else {"update_working_checkpoint"}),
}


# Dispatch markers we count as "real work" for the edit_block_ok counter.
_EDIT_TOOLS = {"file_patch", "file_write"}


# Custom tool schema for ask_explorer — spawns a read-only GA subagent
# (uses GA's --bg flag pattern, commit bc5d1ea). Injected into the
# doer's tools_schema at run_doer_via_ga time.
_ASK_EXPLORER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_explorer",
        "description": (
            "Spawn a read-only sub-agent to answer a focused exploration "
            "question about the codebase. Returns the sub-agent's final "
            "summary. Use when you need to scan many files without "
            "bloating your own context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Single concrete question; the sub-agent has "
                        "file_read + code_run only and answers in <30 turns."
                    ),
                },
            },
            "required": ["question"],
        },
    },
}


_DOER_GA_PREAMBLE = """You are the AIForge Doer agent operating through GenericAgent.

You MUST modify code so the ticket is implemented. You only get credit when:
  1. At least one file_patch or file_write call against an allowed file.
  2. `mvn -DskipTests compile` exits 0 inside the worktree.
  3. Every Acceptance bullet's identifier appears in the new file content.

==== CRITICAL — FILE-EDITING IS THE PRIMARY TOOL ====
The Planner has already identified the files you need. They are listed
verbatim under `## Allowed files`. Trust the list. Open those files with
file_read and edit them with file_patch / file_write. That's the job.

Do NOT use `code_run` to grep / find / ls / locate files. Those discovery
commands burn turns and produce nothing the harness scores. file_read
the allowed paths directly. If a path appears wrong, ask_explorer once
(see tool list); do not fall into a grep/find loop.

If after turn 3 you still have edit_block_ok=0 you are off-track. STOP
exploring. file_read your allowed files. file_patch the change.

Hard rules:
- Edit ONLY files listed in the ## Allowed files section. Writes outside
  that list are blocked by the harness ScopeGuard.
- Do NOT call `ask_user`. Do NOT call `start_long_term_update`. Do NOT
  call any web tool. The harness will reject those calls.
- code_run is for `mvn compile` ONLY (and only AFTER you've patched).
  No find / grep / ls / cat — read files via file_read instead.
- Use file_patch for narrow diffs (preferred). Use file_write only for
  brand-new files or full rewrites.
- Compile ONCE at the very end, AFTER every patch is applied — not
  after each individual file_patch. Doing mvn between edits doubles
  wall-clock (mvn full compile = 60-180s). The harness only checks
  the LAST compile for green/red.
- If the final compile fails, read the error and fix in ONE more
  file_patch + ONE more compile. Maximum two compile calls per run.
- Do NOT emit `<summary>` until the final compile is green AND at
  least one file_patch has succeeded.

Work in the provided worktree path. Every code_run command must
`cd <worktree>` first.

Standard fast workflow (do this exactly):
  1. file_read each entry under `## Allowed files`.
  2. file_patch ALL the changes required by the acceptance criteria
     (often across multiple files: controller + service + repo).
     Apply every patch BEFORE compiling.
  3. ONE code_run `cd <worktree> && mvn -DskipTests compile` — only
     after every file_patch is in. mvn is the most expensive call;
     do it as few times as possible.
  4. On BUILD SUCCESS: emit `<summary>BUILD SUCCESS — <files patched></summary>` and STOP.
  5. On BUILD FAILURE: read the compile error, ONE more file_patch
     to fix it, ONE more mvn compile. Hard cap: two mvn compiles per run.
"""


def _build_user_input(ticket: object, plan_text: str, worktree_path: str,
                      allowed: set[str]) -> str:
    body = getattr(ticket, "body", "") or ""
    title = getattr(ticket, "title", "") or ""
    allowed_list = sorted(allowed)
    allowed_block = (
        "\n".join(f"- {p}" for p in allowed_list)
        if allowed else "(no scope constraint)"
    )
    # Aider RepoMap digest — PageRank tree-sitter signatures (~1024 tok).
    aider_block = aider_digest(
        worktree_path,
        chat_files=allowed_list,
        token_budget=int(os.environ.get("AIFORGE_AIDER_REPOMAP_TOKENS", "1024")),
    )
    aider_section = (
        f"## Code map (Aider RepoMap, ranked by PageRank)\n{aider_block}\n\n"
        if aider_block else ""
    )
    # Graphify + tree-sitter Cypher — neighbour symbols of allowed files.
    neighbours_block = graph_neighbours(
        allowed_list,
        limit=int(os.environ.get("AIFORGE_DOER_NEIGHBOURS_LIMIT", "30")),
    )
    neighbours_section = (
        f"## Neighbour symbols (Neo4j: Graphify + tree-sitter)\n"
        f"{neighbours_block}\n\n" if neighbours_block else ""
    )
    return (
        f"## Worktree\n`{worktree_path}` — every command must run there.\n\n"
        f"## Ticket\n{title}\n\n"
        f"{body}\n\n"
        f"## Allowed files (write-tool ScopeGuard)\n{allowed_block}\n\n"
        f"{aider_section}"
        f"{neighbours_section}"
        f"## Planner notes\n{plan_text or '(none)'}\n\n"
        f"## REQUIRED workflow — DO NOT SKIP STEPS\n"
        f"1. **file_read EACH file under '## Allowed files'** "
        f"(absolute paths under `{worktree_path}`). Do NOT grep/find — "
        f"the file list is final.\n"
        f"2. **file_patch the change required by the acceptance criteria.** "
        f"You MUST emit at least one successful file_patch (or "
        f"file_write) call. The harness rejects runs with "
        f"edit_block_ok=0. Aim to land the first patch by turn 3.\n"
        f"3. code_run `cd {worktree_path} && mvn -DskipTests compile` "
        f"AFTER the patch lands. mvn before editing is a NO-OP "
        f"(original tree already compiles).\n"
        f"4. End with a single `<summary>` tag naming the modified "
        f"file(s) and quoting the BUILD SUCCESS line.\n\n"
        f"## ANTI-PATTERNS — these waste turns and earn no credit\n"
        f"- Running `find` / `grep` / `ls` via code_run. The Planner "
        f"already supplied paths. file_read them.\n"
        f"- Reading the same file twice. Cache it.\n"
        f"- Running mvn before any patch lands.\n"
        f"- Running mvn between every individual file_patch. Apply "
        f"  ALL patches first, then ONE compile. mvn is 60-180s per run.\n"
        f"- More than two mvn compile calls in a single run.\n"
        f"- Emitting <summary> with edit_block_ok=0."
    )


# _ParentShim retained as alias for backwards-compat with any external
# caller; new code should import ParentShim from ga_compat directly.
_ParentShim = ParentShim


def _make_handler_class():
    """Lazy-import GA at call time so module import doesn't require GA on path."""
    ga = import_ga()
    StepOutcome = ga["StepOutcome"]
    GenericAgentHandler = ga["GenericAgentHandler"]

    class AiForgeDoerHandler(GenericAgentHandler):  # type: ignore[misc]
        """Subclass that wires AIForge ScopeGuard + tool deny-list into GA."""

        def __init__(self, parent, scope_guard: ScopeGuard,
                     counters: dict, last_history=None,
                     cwd: str = "./temp") -> None:
            super().__init__(parent, last_history=last_history, cwd=cwd)
            self._scope_guard = scope_guard
            self._counters = counters
            self._chunks: list[str] = []

        def _get_abs_path(self, path: str) -> str:
            abs_path = super()._get_abs_path(path)
            try:
                self._scope_guard.check(abs_path)
            except ScopeViolation:
                # Don't raise inside path resolution — GA dispatches before
                # we yield the rejection. Stash the violation so the tool
                # method itself returns an error StepOutcome.
                self._violation = abs_path
                return abs_path
            self._violation = None
            return abs_path

        def tool_before_callback(self, tool_name, args, response):
            if tool_name in _FORBIDDEN_GA_TOOLS:
                yield (f"[Doer harness] tool '{tool_name}' is forbidden for "
                       f"this role; pick file_patch / file_write / code_run.\n")
                # We can't return a StepOutcome from tool_before_callback in
                # GA's protocol — the actual do_<tool> method still runs.
                # Override do_ask_user / do_start_long_term_update below
                # for hard rejection.
            return None

        # Hard rejection for forbidden tools — overrides GA's do_* methods.
        def do_ask_user(self, args, response):  # type: ignore[override]
            yield "[Doer harness] ask_user is forbidden — re-attempt the edit.\n"
            return StepOutcome(
                {"status": "error", "msg": "ask_user forbidden for Doer"},
                next_prompt=("Do not call ask_user. Use file_read / file_patch / "
                             "code_run to make progress on the ticket."),
            )

        def do_start_long_term_update(self, args, response):  # type: ignore[override]
            yield "[Doer harness] start_long_term_update is forbidden.\n"
            return StepOutcome(
                {"status": "error", "msg": "memory updates are the Learner's job"},
                next_prompt=("Do not call start_long_term_update. Continue editing "
                             "until compile is green, then stop."),
            )

        def do_code_run(self, args, response):  # type: ignore[override]
            """Wrap GA's code_run so we cap mvn invocations to 2 per ticket
            (apply-all-patches + retry-on-fail) and refuse grep/find/ls
            shell scripts the prompt forbids. Caps stop the doer
            burning 2-4 minutes per redundant mvn cycle."""
            # GA reads code from args.code OR args.script OR extracts a
            # ```...``` block from the response. Mirror that order so
            # our gating sees what GA will actually run.
            code = args.get("code") or args.get("script") or ""
            if not code and response:
                # Quick fallback: scan the response for the LAST fenced
                # block. We don't need surgical extraction — substring
                # match against our forbidden tokens is enough.
                code = response
            low = (code or "").lower()
            # Refuse `find` / `grep` / `ls` / `locate` / `cat` shell scripts —
            # the prompt directs the model to use file_read for files the
            # Planner already enumerated. mvn output piped through `grep`
            # is OK so we keep `mvn` an escape hatch.
            for forbidden in ("grep ", "find ", "ls ", "locate ", "cat "):
                # Match at line start or after whitespace/`;`/`&&`/`|` —
                # avoids matching against random words like "false" or
                # "Class" containing the substring.
                import re as _re
                if _re.search(rf"(?:^|[\s;|&]){_re.escape(forbidden)}", low):
                    if "mvn" not in low:
                        yield ("[Doer harness] code_run rejected — "
                               "no shell discovery (grep/find/ls/cat). "
                               "Use file_read on the allowed paths.\n")
                        return StepOutcome(
                            {"status": "error",
                             "msg": "shell discovery forbidden; use file_read"},
                            next_prompt=("Use file_read on each allowed file. "
                                         "Do not grep/find/ls/cat from code_run."),
                        )
            if "mvn" in low:
                self._counters["mvn_runs"] = (
                    self._counters.get("mvn_runs", 0) + 1
                )
                if self._counters["mvn_runs"] > 2:
                    yield ("[Doer harness] mvn cap hit (2 compiles max). "
                           "Stop running mvn — finalize edits or summary.\n")
                    return StepOutcome(
                        {"status": "error",
                         "msg": "mvn cap exceeded (2 per ticket)"},
                        next_prompt=("You've already run mvn twice. Either "
                                     "the build is green and you should "
                                     "<summary>, or your patches are wrong "
                                     "and need a different approach. Do not "
                                     "compile again."),
                    )
            yield from super().do_code_run(args, response)

        def do_ask_explorer(self, args, response):  # type: ignore[override]
            """Spawn a read-only GA subprocess to answer a focused
            question. Uses GA's `agentmain.py --task <name> --bg --input
            <q>` pattern (commit bc5d1ea). Polls output*.txt for
            [ROUND END] sentinel and returns the result.
            """
            import subprocess
            question = (args.get("question") or "").strip()
            if not question:
                yield "[ask_explorer] empty question, skipping.\n"
                return StepOutcome(
                    {"status": "error", "msg": "question required"},
                    next_prompt="ask_explorer needs a `question` argument.",
                )
            ga_path = ga_dir()
            sub_id = f"explorer-{int(time.time()*1000)}"
            sub_dir = os.path.join(ga_path, "temp", sub_id)
            os.makedirs(sub_dir, exist_ok=True)
            yield f"[ask_explorer] spawning sub-agent {sub_id}\n"
            try:
                subprocess.run(
                    [sys.executable, os.path.join(ga_path, "agentmain.py"),
                     "--task", sub_id, "--bg", "--input", question,
                     "--llm_no", "0", "--verbose=False"],
                    cwd=ga_path,
                    timeout=15,
                    capture_output=True,
                    env={**os.environ, "GA_LANG": "en"},
                )
            except Exception as exc:
                yield f"[ask_explorer] spawn failed: {exc}\n"
                return StepOutcome(
                    {"status": "error", "msg": f"spawn failed: {exc}"},
                    next_prompt="ask_explorer process failed to start.",
                )
            # Poll for ROUND END sentinel; cap at 5 min wall.
            out_path = os.path.join(sub_dir, "output.txt")
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(5)
                if os.path.exists(out_path):
                    text = ""
                    try:
                        text = open(out_path, encoding="utf-8").read()
                    except Exception:
                        text = ""
                    if "[ROUND END]" in text:
                        # Strip the protocol-end sentinel; cap at 4KB.
                        answer = text.split("[ROUND END]")[0][-4000:]
                        yield f"[ask_explorer] sub-agent finished\n"
                        return StepOutcome(
                            {"status": "ok", "answer": answer},
                            next_prompt=None,
                        )
            yield "[ask_explorer] sub-agent timeout (5 min)\n"
            return StepOutcome(
                {"status": "timeout", "msg": "explorer timed out"},
                next_prompt="ask_explorer did not return in 5 minutes.",
            )

        def do_file_patch(self, args, response):  # type: ignore[override]
            outcome_gen = super().do_file_patch(args, response)
            outcome = yield from outcome_gen
            if isinstance(outcome.data, dict) and outcome.data.get("status") in (
                "success", "ok",
            ):
                self._counters["edit_block_ok"] = (
                    self._counters.get("edit_block_ok", 0) + 1
                )
            return outcome

        def do_file_write(self, args, response):  # type: ignore[override]
            outcome_gen = super().do_file_write(args, response)
            outcome = yield from outcome_gen
            if isinstance(outcome.data, dict) and outcome.data.get("status") == "success":
                self._counters["edit_block_ok"] = (
                    self._counters.get("edit_block_ok", 0) + 1
                )
            return outcome

        def do_code_run(self, args, response):  # type: ignore[override]
            # Detect mvn compile invocations and bump the compile counter
            # when the result content carries a BUILD SUCCESS line.
            outcome_gen = super().do_code_run(args, response)
            outcome = yield from outcome_gen
            text = ""
            if isinstance(outcome.data, dict):
                text = json.dumps(outcome.data, default=str)
            else:
                text = str(outcome.data or "")
            if "mvn" in (args.get("script") or "") + (args.get("code") or ""):
                if "BUILD SUCCESS" in text and "BUILD FAILURE" not in text:
                    self._counters["compile_green"] = (
                        self._counters.get("compile_green", 0) + 1
                    )
                elif "BUILD FAILURE" in text or "[ERROR]" in text:
                    self._counters["last_compile_error"] = text[-1500:]
            return outcome

    return AiForgeDoerHandler, StepOutcome


def _doer_llm_config() -> dict:
    """The text-protocol cfg for GA's LLMSession. Matches ``oai_doer_config``
    in mykey.py on the NUC. We hard-code here so this module doesn't need
    GA's mykey at import time — GA reads its own mykey for default config,
    but ``LLMSession(cfg=...)`` accepts an explicit cfg dict and overrides."""
    base_url = os.environ.get(
        "AIFORGE_DOER_BASE_URL", "http://127.0.0.1:1234"
    )
    model = os.environ.get(
        "AIFORGE_DOER_MODEL",
        "/Users/manikanta/.lmstudio/models/lmstudio-community/Qwen3-Coder-Next-MLX-4bit",
    )
    cfg: dict = {
        "name": "mlx-doer",
        "apikey": os.environ.get("AIFORGE_DOER_API_KEY", "sk-local"),
        "apibase": base_url.rstrip("/").rstrip("/v1"),
        "model": model,
        "api_mode": "chat_completions",
        "max_retries": 2,
        "connect_timeout": 10,
        "read_timeout": 180,
        "context_win": int(os.environ.get("AIFORGE_DOER_CTX", "28000")),
        "max_tokens": int(os.environ.get("AIFORGE_DOER_MAX_TOKENS", "8192")),
        "temperature": float(os.environ.get("AIFORGE_DOER_TEMP", "0.2")),
    }
    # Optional knobs — only added when explicitly set so we don't push
    # model-specific keys onto a model that doesn't accept them.
    if os.environ.get("AIFORGE_DOER_TOP_P"):
        cfg["top_p"] = float(os.environ["AIFORGE_DOER_TOP_P"])
    if os.environ.get("AIFORGE_DOER_TOP_K"):
        cfg["top_k"] = int(os.environ["AIFORGE_DOER_TOP_K"])
    # Thinking-mode toggle for models that support it (Gemma 4, Qwen3.x
    # with --enable-thinking, etc.). chat_template_kwargs flow through
    # to the underlying chat template. Note: Gemma 4's default chat
    # template has thinking ON — so AIFORGE_DOER_THINK=0 has to send
    # an explicit `enable_thinking: false` rather than just omitting
    # the kwarg (which is the right behaviour for non-thinking models
    # but a silent NOOP on Gemma 4).
    think_env = os.environ.get("AIFORGE_DOER_THINK")
    if think_env == "1":
        cfg["chat_template_kwargs"] = {"enable_thinking": True}
    elif think_env == "0":
        cfg["chat_template_kwargs"] = {"enable_thinking": False}
    return cfg



def _load_tools_schema(ga_dir: str | None = None) -> list[dict]:
    """Backwards-compat wrapper. Delegates to ga_compat.load_tools_schema."""
    return load_tools_schema(filter_drop=_FORBIDDEN_GA_TOOLS)


def run_doer_via_ga(
    ticket: object,
    worktree_path: str,
    plan_text: str = "",
    max_turns: int = 30,
    log: object | None = None,
) -> dict:
    """Run the Doer through GenericAgent's text-protocol agent loop.

    Returns a dict with the same shape as ``run_smolagents_doer``:
    ``{stop_reason, has_commented, turns, wall_s, summary, ...}``.
    """
    t_start = time.time()
    identifier = getattr(ticket, "identifier", "?")
    ticket_id = getattr(ticket, "id", None)

    # All GA symbols come through ga_compat — single point of upgrade.
    try:
        ga = import_ga()
    except Exception as exc:
        emit(log, "ga_runner.import_failed", ticket=identifier,
             err=str(exc)[:200], ga_compat=GA_COMPAT_VERSION,
             ga_sha=ga_sha())
        return {
            "stop_reason": "exception",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": f"GA import failed: {exc}",
        }
    agent_runner_loop = ga["agent_runner_loop"]
    LLMSession = ga["LLMSession"]
    ToolClient = ga["ToolClient"]
    _ga_path = ga_dir()

    HandlerCls, StepOutcome = _make_handler_class()

    # Allowlist + scope guard for write tools.
    body = getattr(ticket, "body", "") or ""
    allowed = parse_allowed_files(body)
    scope_guard = ScopeGuard(allowed)

    # GA needs a per-task scratch dir for parent.task_dir reads (_keyinfo, _intervene).
    task_dir = os.path.join(_ga_path, "temp", f"aiforge-{identifier}-{int(t_start)}")
    os.makedirs(task_dir, exist_ok=True)
    parent = _ParentShim(task_dir=task_dir)

    counters = {"edit_block_ok": 0, "compile_green": 0}
    handler = HandlerCls(parent, scope_guard=scope_guard,
                         counters=counters, last_history=[],
                         cwd=worktree_path)

    # GA plan mode — when the planner produced a checkbox plan, point
    # GA's plan-mode at it. GA tracks `[ ]`/`[x]` checkboxes in the
    # plan file, blocks premature "task complete" claims, auto-exits
    # when all boxes are checked. See ga.py:417-484.
    plan_md_path = os.path.join(worktree_path, ".aiforge", "plan.md")
    plan_mode_active = False
    if os.path.isfile(plan_md_path):
        try:
            handler.enter_plan_mode(plan_md_path)
            plan_mode_active = True
            emit(log, "ga_runner.plan_mode_on",
                 ticket=identifier, plan_path=plan_md_path)
        except Exception as exc:
            emit(log, "ga_runner.plan_mode_skip",
                 ticket=identifier, error=str(exc)[:200])

    cfg = _doer_llm_config()
    session = LLMSession(cfg=cfg)
    client = ToolClient(session)

    tools_schema = _load_tools_schema()
    # Inject ask_explorer (custom tool, not in GA's stock schema).
    if os.environ.get("AIFORGE_DOER_ASK_EXPLORER", "1") == "1":
        tools_schema = list(tools_schema) + [_ASK_EXPLORER_SCHEMA]
    user_input = _build_user_input(ticket, plan_text, worktree_path, allowed)
    if plan_mode_active:
        user_input += (
            f"\n\n## Plan mode\nGA plan-mode is active. Pace yourself "
            f"by the checkboxes in {plan_md_path}. After each step "
            f"completes, edit the plan file in-place and replace `[ ]` "
            f"with `[x]` for that step. Plan mode exits automatically "
            f"when all boxes are checked."
        )
    chunks: list[str] = []
    turn_count = 0

    # Hybrid memory: monkey-patch GA's get_global_memory so it returns
    # filesystem text + Neo4j L2 facts (top-K, scoped to ticket + files).
    # GA reads the patched function on every call to system-prompt build
    # AND every 10-turn re-injection (ga.py:533).
    set_fetch_context(
        ticket_id=identifier,
        role="doer",
        file_paths=sorted(allowed),
        query_text=" ".join(sorted(allowed)) + " " + (
            getattr(ticket, "title", "") or ""
        ),
    )
    # Monkey-patch llmcore.tryparse to be lenient on raw newlines + literal
    # control chars inside JSON string values. Coder-Next emits Anthropic
    # tool_use JSON with raw \n inside old_content / new_content — strict
    # json.loads rejects, GA logs '[Warn] Failed to parse tool_use JSON',
    # file_patch never executes, edit_block_ok stays 0 forever.
    try:
        import llmcore as _llmcore  # type: ignore
        if not getattr(_llmcore, "_aiforge_tryparse_patched", False):
            import json as _json
            _orig_tryparse = _llmcore.tryparse

            def _lenient_tryparse(json_str: str):
                try:
                    return _json.loads(json_str, strict=False)
                except Exception:
                    pass
                # Repair raw newlines / tabs inside double-quoted string
                # values so json.loads can swallow them.
                import re as _re
                def _esc(m: _re.Match) -> str:
                    s = m.group(0)
                    return (s.replace("\\", "\\\\")
                             .replace("\n", "\\n")
                             .replace("\r", "\\r")
                             .replace("\t", "\\t"))
                # Match contents of every "..." pair. Tolerates already-
                # escaped quotes inside strings.
                fixed = _re.sub(
                    r'"((?:[^"\\]|\\.)*)"',
                    lambda m: '"' + _esc(m.group(1)) + '"',
                    json_str, flags=_re.DOTALL,
                )
                try:
                    return _json.loads(fixed, strict=False)
                except Exception:
                    return _orig_tryparse(json_str)

            _llmcore.tryparse = _lenient_tryparse
            _llmcore._aiforge_tryparse_patched = True
            emit(log, "ga_runner.tryparse_patched", ticket=identifier)
    except Exception as exc:
        emit(log, "ga_runner.tryparse_patch_skipped",
             ticket=identifier, error=str(exc)[:200])

    try:
        import ga as _ga_mod  # type: ignore
        if not getattr(_ga_mod, "_aiforge_patched", False):
            _orig_get = _ga_mod.get_global_memory

            def _hybrid_get_global_memory():
                base = ""
                try:
                    base = _orig_get() or ""
                except Exception:
                    base = ""
                try:
                    extra = fetch_facts_text(
                        k=int(os.environ.get("AIFORGE_DOER_FACTS_K", "5"))
                    )
                except Exception:
                    extra = ""
                if not extra:
                    return base
                return f"{base}\n\n{extra}\n"

            _ga_mod.get_global_memory = _hybrid_get_global_memory  # type: ignore
            _ga_mod._aiforge_patched = True  # type: ignore
            emit(log, "ga_runner.memory_patched",
                 ticket=identifier, mode="hybrid_fs_plus_neo4j")
    except Exception as exc:
        emit(log, "ga_runner.memory_patch_skipped",
             ticket=identifier, error=str(exc)[:200])

    emit(log, "ga_runner.start", ticket=identifier, max_turns=max_turns,
         allowed_count=len(allowed))

    final_summary = ""
    try:
        gen = agent_runner_loop(
            client, _DOER_GA_PREAMBLE, user_input, handler,
            tools_schema, max_turns=max_turns, verbose=False,
            initial_user_content=None,
        )
        for chunk in gen:
            chunks.append(str(chunk))
            # GA emits "LLM Running (Turn N) ..." at the top of every turn.
            m = re.search(r"LLM Running \(Turn (\d+)\)", str(chunk))
            if m:
                turn_count = max(turn_count, int(m.group(1)))
        final_summary = "".join(chunks[-12:])[-3500:]
    except ScopeViolation as exc:
        emit(log, "ga_runner.scope_violation", ticket=identifier, path=exc.path)
        if ticket_id is not None:
            tickets_mod.add_event(
                ticket_id, "doer", "error",
                body=f"GA scope violation: {exc}",
                metadata={"stop_reason": "scope_violation", "backend": "genericagent"},
            )
        return {
            "stop_reason": "scope_violation",
            "has_commented": False,
            "turns": turn_count,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
        }
    except Exception as exc:
        emit(log, "ga_runner.exception", ticket=identifier, err=str(exc)[:300])
        if ticket_id is not None:
            tickets_mod.add_event(
                ticket_id, "doer", "error",
                body=f"GA exception: {exc}",
                metadata={"stop_reason": "exception", "backend": "genericagent"},
            )
        return {
            "stop_reason": "exception",
            "has_commented": False,
            "turns": turn_count,
            "wall_s": round(time.time() - t_start, 2),
            "summary": f"{type(exc).__name__}: {exc}",
        }

    # Verify compile manually (cheap — GA may have run it but we want truth).
    mvn_proc = subprocess.run(
        ["mvn", "-q", "-DskipTests", "compile"],
        cwd=worktree_path, capture_output=True, text=True, check=False,
        timeout=900,
    )
    compile_pass = mvn_proc.returncode == 0
    if compile_pass:
        counters["compile_green"] = max(1, counters.get("compile_green", 0))
    else:
        counters["last_compile_error"] = (
            (mvn_proc.stdout or "") + (mvn_proc.stderr or "")
        )[-1500:]

    # Diff stat.
    diff_proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=worktree_path, capture_output=True, text=True, check=False,
    )
    changed_files = [ln for ln in diff_proc.stdout.splitlines() if ln.strip()]

    edits_ok = counters.get("edit_block_ok", 0)
    if not compile_pass or edits_ok < 1:
        if ticket_id is not None:
            tickets_mod.add_event(
                ticket_id, "doer", "error",
                body=(f"GA Doer ended without green compile or edits "
                      f"(edits={edits_ok}, compile_green={int(compile_pass)}).\n\n"
                      f"Last compile error:\n{counters.get('last_compile_error', '')[-800:]}\n\n"
                      f"Trace tail:\n{final_summary[:1500]}"),
                metadata={
                    "stop_reason": "checklist_fail",
                    "backend": "genericagent",
                    "counters": counters,
                    "compile_error": counters.get("last_compile_error", "")[:1500],
                },
            )
        emit(log, "ga_runner.checklist_fail", ticket=identifier,
             counters=counters, turns=turn_count)
        return {
            "stop_reason": "checklist_fail",
            "has_commented": False,
            "turns": turn_count,
            "wall_s": round(time.time() - t_start, 2),
            "summary": final_summary,
            "counters": counters,
        }

    # Commit ONLY here. Push + PR are deferred to the AiForgePublishAgent
    # so the deterministic feedback gate (and integration smoke) can
    # veto a doomed change before it lands on origin. Setting
    # AIFORGE_PUBLISH_BYPASS_GATE=1 reverts to the legacy atomic path
    # for emergency manual flows.
    bypass = os.environ.get("AIFORGE_PUBLISH_BYPASS_GATE", "0") == "1"
    pub: dict = {"commit_sha": None, "pushed": False, "pr_url": None}
    try:
        if bypass:
            from .orchestrator_bridge import _git_commit_push_pr
            pub = _git_commit_push_pr(
                ticket, worktree_path, final_summary, changed_files, log,
            )
        else:
            from .orchestrator_bridge import _git_commit_only
            commit_res = _git_commit_only(
                ticket, worktree_path, changed_files, log,
            )
            pub.update(commit_res)
            emit(log, "ga_runner.publish_deferred", ticket=identifier,
                 commit_sha=pub.get("commit_sha"))
    except Exception as exc:
        emit(log, "ga_runner.publish_failed", ticket=identifier, err=str(exc)[:200])

    if ticket_id is not None:
        comment_body = (final_summary or "(no summary)")[:3500]
        if pub.get("pr_url"):
            comment_body = f"{comment_body}\n\nPR: {pub['pr_url']}"
        elif pub.get("commit_sha"):
            comment_body = f"{comment_body}\n\nCommit: {pub['commit_sha']} (push pending gate)"
        tickets_mod.add_event(
            ticket_id, "doer", "comment",
            body=comment_body[:4000],
            metadata={
                "source": "genericagent_text_protocol",
                "files_changed": changed_files,
                "counters": counters,
                **{k: v for k, v in pub.items() if v is not None},
            },
        )

    emit(log, "ga_runner.done", ticket=identifier,
         turns=turn_count, edits=edits_ok,
         compile_green=int(compile_pass),
         files_changed=len(changed_files),
         commit_sha=pub.get("commit_sha"),
         pushed=pub.get("pushed"),
         pr_url=pub.get("pr_url"))
    return {
        "stop_reason": "final_answer",
        "has_commented": True,
        "turns": turn_count,
        "wall_s": round(time.time() - t_start, 2),
        "summary": final_summary,
        "counters": counters,
        "commit_sha": pub.get("commit_sha"),
        "pr_url": pub.get("pr_url"),
    }
