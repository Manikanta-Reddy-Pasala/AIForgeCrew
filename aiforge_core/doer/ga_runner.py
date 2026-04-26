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
# Doer now has the full GA tool surface. ask_user goes through our
# ticket-comment escalation path (do_ask_user override below) instead
# of blocking on stdin. start_long_term_update / web_scan /
# web_execute_js are enabled — let the model self-recover via memory
# notes + Spring docs lookups when it hits unknown APIs.
_FORBIDDEN_GA_TOOLS: set[str] = set()
if os.environ.get("AIFORGE_DOER_KEEP_CHECKPOINT") != "1":
    _FORBIDDEN_GA_TOOLS.add("update_working_checkpoint")


# Dispatch markers we count as "real work" for the edit_block_ok counter.
_EDIT_TOOLS = {"file_patch", "file_write"}


# Custom tool schema for web_search — Gemini 2.5-flash grounded search.
# Calls https://generativelanguage.googleapis.com with `googleSearch` tool
# enabled; Gemini returns a curated answer + citations the Doer can use
# to fix unknown-API errors. Needs AIFORGE_GOOGLE_API_KEY env.
_WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Run a Google-grounded web search via Gemini 2.5-flash. "
            "Returns Gemini's curated answer + top citation URLs. "
            "Use for unknown-API recovery: when 'cannot find symbol "
            "X' compile errors point at a class you don't recognize, "
            "search the official docs and patch with the right API."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Concise search query. Best results: include "
                        "the framework + version + specific API. "
                        "e.g. 'Spring Data MongoDB Aggregation.group "
                        "with sum and count Java example'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


# Custom tool schema for ask_explorer — spawns a read-only GA subagent
# (uses GA's --bg flag pattern, commit bc5d1ea). Injected into the
# doer's tools_schema at run_doer_via_ga time.
_ASK_EXPLORER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_explorer",
        "description": (
            "Spawn read-only sub-agent(s) to answer focused exploration "
            "questions about the codebase. Pass `question` (single) "
            "OR `questions` (list) — list spawns up to 4 sub-agents "
            "concurrently and returns joined summaries. Use when you "
            "need to scan files without bloating your own context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Single concrete question; sub-agent answers "
                        "in <30 turns."
                    ),
                },
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Up to 4 questions. Spawned in parallel — "
                        "use for fan-out exploration "
                        "(e.g. 'find Mongo aggregation examples', "
                        "'find Spring Mongo group syntax', "
                        "'find @GetMapping path conventions')."
                    ),
                },
            },
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

If after turn 3 you still have edit_block_ok=0 you are off-track. STOP
exploring. file_read your allowed files. file_patch the change.

==== TOOL CHEAT SHEET ====

** PARALLELISM (mandatory — Claude CLI / Cursor style) **
When you need to read OR explore N files, do ALL of them in ONE turn:
  - batch calls=[{tool: file_read, args: {...}}, {tool: file_read, ...},
                  {tool: glob, ...}, {tool: grep, ...}]
                       → fans out read-side tools concurrently. ONE turn
                         = N parallel reads. NEVER read files one-per-turn
                         when the ticket already lists them under
                         '## Edit targets' or '## Reference files'.
  - bulk_edit edits=[…]  → as below, but for writes.
  - dispatch_subagent multi=[…] → up to 4 read-only explorers in parallel
                         when you need narrative answers across the repo.

Do NOT serialize: one file_read per turn = wasted context. The first
LLM turn after task receipt should be a single `batch` covering every
file under '## Edit targets' AND '## Reference files'.

- file_read PATH       → returns line-numbered content; cached per run.
                         Don't re-request the same file. Prefer batch
                         when reading multiple.
- file_patch / file_write → make ONE edit. file_patch returns a diff
                         block AFTER the edit so you can verify it
                         landed correctly.
- bulk_edit edits=[...]→ apply N file edits atomically in ONE turn.
                         Use when ticket touches several files
                         (controller + service + repo + DTO). Any
                         single failure rolls the whole batch back
                         to git HEAD. 4-5 turns → 1 turn.
- java_refactor recipe → invoke an OpenRewrite recipe via mvn
                         (e.g. ChangePackage, RenameMethod,
                         RemoveUnusedImports, OrderImports). Cheaper
                         than hand-patching every import; recipe
                         engine knows Java syntax.
- glob PATTERN         → fast file listing (ripgrep). e.g.
                         '**/*Controller.java'.
- grep REGEX [glob]    → search file contents (ripgrep). Returns
                         path:line:content rows. Use this BEFORE
                         ask_explorer — it's instant.
- bash COMMAND         → persistent shell session. cwd survives
                         across calls. Use for mvn / git / curl.
                         Default 60s, max 600s.
- lint                 → run configured lint command (mvn checkstyle / ruff /
                         tsc), returns errors. Use after compile is green.
- tests                → run unit tests (mvn test / pytest), returns
                         JUnit failures. Use when ticket touches behaviour.
- undo {mode,path}     → roll back ONE file (mode=last_edit) or the
                         last commit (mode=last_commit). Escape hatch.
- web_search QUERY     → Gemini-grounded search. Use on
                         'cannot find symbol' errors.
- ask_explorer Q       → spawn read-only sub-agent for broad
                         exploration. Slower than grep; use when
                         you need narrative context.
- ask_user QUESTION    → escalate to operator (logs question on
                         the ticket). Last resort.

Hard rules:
- Edit ONLY files listed in the ## Allowed files section. Writes outside
  that list are blocked by the harness ScopeGuard.
- code_run is for `mvn compile` ONLY (and only AFTER you've patched).
  No find / grep / ls / cat — read files via file_read instead.
- web_search (Gemini grounded) IS the preferred lookup tool for
  unknown-API errors. Pass a precise query like
  'Spring Data MongoDB Aggregation.group sum count Java example' —
  Gemini answers with the right API + citation URLs. Use this on
  every 'cannot find symbol' / 'method not found' / 'incompatible
  types' compile error you don't immediately recognize.
- web_scan is the fallback for fetching a specific docs page when
  web_search citations point you at one. Don't loop on a wrong API.
- start_long_term_update IS allowed for one-line patterns you hit
  repeatedly (e.g. 'Spring Mongo grouping uses Aggregation.group not
  Expressions.wrap'). Future tickets benefit from your memory.
- ask_user IS allowed when you've truly exhausted options. It logs
  the question to the ticket — operator answers and resubmits. Use
  it sparingly; prefer ask_explorer + web_scan first.
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
    # Parallel context fetch — Aider RepoMap (CPU/disk-heavy, ~16s
    # cold) and Graphify graph_neighbours (Neo4j round-trip, ~1s)
    # run concurrently. Saves ~15s on every doer start.
    import concurrent.futures as _cf
    aider_block = ""
    neighbours_block = ""
    with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
        _aider_fut = _ex.submit(
            aider_digest, worktree_path,
            chat_files=allowed_list,
            token_budget=int(os.environ.get("AIFORGE_AIDER_REPOMAP_TOKENS", "1024")),
        )
        _neighbours_fut = _ex.submit(
            graph_neighbours, allowed_list,
            limit=int(os.environ.get("AIFORGE_DOER_NEIGHBOURS_LIMIT", "30")),
        )
        try:
            aider_block = _aider_fut.result(timeout=60) or ""
        except Exception:
            aider_block = ""
        try:
            neighbours_block = _neighbours_fut.result(timeout=15) or ""
        except Exception:
            neighbours_block = ""
    aider_section = (
        f"## Code map (Aider RepoMap, ranked by PageRank)\n{aider_block}\n\n"
        if aider_block else ""
    )
    neighbours_section = (
        f"## Neighbour symbols (Neo4j: Graphify + tree-sitter)\n"
        f"{neighbours_block}\n\n" if neighbours_block else ""
    )
    # Per-repo conventions — Aider's CONVENTIONS.md analogue at
    # .aiforge/CONVENTIONS.md.
    try:
        from .ga_tools import conventions as _conv
        conventions_section = _conv.section_for_prompt(worktree_path)
    except Exception:
        conventions_section = ""
    # Read-only files list (Aider --read-only analogue).
    try:
        from .ga_tools import readonly as _ro
        ro_set = _ro.collect(worktree_path, body)
    except Exception:
        ro_set = set()
    readonly_section = (
        "## Read-only files (visible, do NOT edit)\n"
        + "\n".join(f"- {p}" for p in sorted(ro_set))
        + "\n\n"
    ) if ro_set else ""
    # Per-project standards (Neo4j :Repo + worktree YAML) — render
    # the manifest into the prompt so the model sees lint/test/
    # forbidden_patterns/conventions directly. KISS: one block.
    standards_section = ""
    try:
        from aiforge_core.runtime import repo_standards as _rs
        _repo_name = os.path.basename(os.path.normpath(worktree_path))
        _std = _rs.get(_repo_name, worktree=worktree_path)
        standards_section = (
            f"## Project standards (auto-loaded from Neo4j :Repo)\n"
            f"```\n{_rs.render(_std)}\n```\n\n"
        )
        if _std.forbidden_patterns:
            standards_section += (
                "**Forbidden patterns** — do NOT introduce these:\n"
                + "\n".join(f"  - `{p}`" for p in _std.forbidden_patterns[:20])
                + "\n\n"
            )
        if _std.acceptance_criteria:
            standards_section += (
                "**Acceptance criteria**:\n"
                + "\n".join(f"  - {a}" for a in _std.acceptance_criteria[:20])
                + "\n\n"
            )
    except Exception:
        pass
    # UnifiedContext — single read API across all 8 sources. Keyed off
    # ticket TEXT (not allowed_files), so empty allowed_list no longer
    # collapses context. Best-effort.
    unified_section = ""
    try:
        from aiforge_core.context import UnifiedContext as _UC
        _bundle = _UC().for_doer(ticket, token_budget=4500)
        _rendered = _bundle.render()
        if _rendered:
            unified_section = (
                "## Auto-context (UnifiedContext — ticket-text keyed)\n"
                f"{_rendered}\n\n"
            )
    except Exception:
        unified_section = ""
    return (
        f"## Worktree\n`{worktree_path}` — every command must run there.\n\n"
        f"## Ticket\n{title}\n\n"
        f"{body}\n\n"
        f"{unified_section}"
        f"{standards_section}"
        f"{conventions_section}"
        f"## Allowed files (write-tool ScopeGuard)\n{allowed_block}\n\n"
        f"{readonly_section}"
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
        f"## RECOVERY — when you hit a compile error\n"
        f"1. READ the error carefully. 'cannot find symbol' = wrong "
        f"API, NOT a missing import most of the time.\n"
        f"2. ask_explorer 'show me example usage of <ClassName> in "
        f"this repo' — copy the working pattern.\n"
        f"3. If repo has no example, web_scan the official docs URL "
        f"(e.g. docs.spring.io / javadoc.io). Find the right method "
        f"signature.\n"
        f"4. Patch with the correct API. ONE more compile.\n"
        f"5. Truly stuck (e.g. acceptance contradicts existing API)? "
        f"   Call ask_user with a specific question — it logs to the "
        f"   ticket for the operator.\n\n"
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
            # Plan Mode write-tool guard. When active, write tools are
            # short-circuited via a per-handler reject flag the do_*
            # method consults. tool_before_callback can't itself return
            # a StepOutcome, so we stash a bool the writer methods read
            # and bail on.
            from .ga_tools import plan_mode as _pm
            if _pm.is_active(self) and tool_name in _pm.WRITE_TOOLS:
                self._plan_mode_reject_next = True  # type: ignore[attr-defined]
                yield _pm.reject_message(tool_name) + "\n"
            # Perf recorder — start wall-clock for this dispatch.
            # tool_after_callback emits the step.
            import time as _t
            self._aiforge_tool_t0 = _t.time()  # type: ignore[attr-defined]
            self._aiforge_tool_name = tool_name  # type: ignore[attr-defined]
            return None

        def tool_after_callback(self, tool_name, args, response, ret):
            """Record per-tool wall_ms via hooks.emit_step. KISS:
            classify the event from tool_name into search / file_read
            / file_write / tool buckets so /api/runtime/perf shows
            useful aggregates without tagging every do_*."""
            import time as _t
            t0 = getattr(self, "_aiforge_tool_t0", None)
            if t0 is None:
                return None
            wall_ms = int((_t.time() - t0) * 1000)
            from .ga_tools import hooks as _hk
            event = "post_tool"
            if tool_name in ("glob", "grep", "search_memory",
                             "unified_memory_query"):
                event = "post_search"
            elif tool_name == "file_read":
                event = "post_file_read"
            elif tool_name in ("file_write", "file_patch", "bulk_edit"):
                event = "post_file_write"
            try:
                _hk.emit_step(event=event, name=tool_name,
                              wall_ms=wall_ms,
                              extra={"args_keys": list(args.keys())})
            except Exception:
                pass
            return None

        # ask_user has TWO paths:
        #   v1 (default): push 'doer_question' ticket event, end run
        #     with awaiting_user, operator replies via ticket comment,
        #     planner picks up the answer on resubmit.
        #   v2 (AIFORGE_DOER_HITL_V2=1): hitl.request_input — pending
        #     row goes to Postgres hitl_pending so the dispatcher
        #     poll can resume the same session via hitl.resume(...)
        #     instead of restarting the Doer from scratch.
        def do_ask_user(self, args, response):  # type: ignore[override]
            question = (args.get("question") or "").strip()
            candidates = args.get("candidates") or []
            ticket_obj = getattr(self.parent, "_aiforge_ticket", None)
            ticket_identifier = (
                getattr(ticket_obj, "identifier", "?") if ticket_obj else "?"
            )

            # HITL v2 path — park via hitl.request_input.
            if os.environ.get("AIFORGE_DOER_HITL_V2", "0") == "1":
                try:
                    from aiforge_core.runtime import hitl as _hitl
                    msg = (
                        f"Doer needs operator input:\n\n{question}\n\n"
                        + (f"Suggested options: {candidates}\n"
                           if candidates else "")
                        + "Reply with `aiforge:answer:<your answer>`."
                    )
                    snapshot = {
                        "ticket": ticket_identifier,
                        "cwd":    self.cwd,
                        "turn":   getattr(self, "current_turn", 0),
                        "candidates": candidates,
                    }
                    pending = _hitl.request_input(
                        msg, ticket=ticket_identifier, snapshot=snapshot,
                    )
                    yield (f"[Doer harness] ask_user parked HITL "
                           f"(pending {pending.id}).\n")
                    return StepOutcome(
                        {"status": "awaiting_user_hitl",
                         "pending_id": pending.id,
                         "msg": "parked via hitl.request_input"},
                        next_prompt=(
                            "Pending operator answer. Stop here — output "
                            "a <summary> reflecting the open question, "
                            "then end. Dispatcher will resume this run "
                            "when an answer lands."
                        ),
                    )
                except Exception as exc:
                    yield f"[Doer harness] hitl_v2 failed: {exc}; falling back to v1\n"

            # v1 path (default) — ticket-comment-then-resubmit.
            if ticket_obj is not None:
                try:
                    body = (
                        f"Doer needs operator input:\n\n{question}\n\n"
                        + (f"Suggested options: {candidates}\n" if candidates else "")
                        + "Reply on this ticket and resubmit."
                    )
                    tickets_mod.add_event(
                        ticket_obj.id, "doer", "doer_question",
                        body=body[:4000],
                        metadata={"question": question[:1000],
                                  "candidates": candidates},
                    )
                except Exception:
                    pass
            yield (f"[Doer harness] ask_user logged on ticket. "
                   f"Question: {question[:200]}\n")
            return StepOutcome(
                {"status": "awaiting_user",
                 "msg": "question logged on ticket"},
                next_prompt=("Operator notified via ticket comment. "
                             "Stop here — output a <summary> reflecting "
                             "the open question, then end."),
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

        def do_web_search(self, args, response):  # type: ignore[override]
            """Thin wrapper — pure logic in tools.web_search.handle()."""
            from .ga_tools import web_search as _ws
            yield f"[web_search] {(args.get('query') or '')[:200]}\n"
            blob = _ws.handle(args)
            if blob.startswith("[web_search]"):
                return StepOutcome(
                    {"status": "error", "msg": blob},
                    next_prompt="web_search failed; try ask_explorer.",
                )
            return StepOutcome(
                blob,
                next_prompt=("Web answer above. Apply the correct API "
                             "in your next file_patch."),
            )

        def do_file_read(self, args, response):  # type: ignore[override]
            """Wrap GA file_read with line numbers + per-run cache.

            ReadTracker stashed on handler._aiforge_reader.
            """
            from .ga_tools.read_tracker import ReadTracker
            reader = getattr(self, "_aiforge_reader", None)
            if reader is None:
                reader = ReadTracker()
                self._aiforge_reader = reader  # type: ignore[attr-defined]
            abs_path = self._get_abs_path(args.get("path", ""))
            blob = reader.read(abs_path)
            yield blob[:400] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_glob(self, args, response):  # type: ignore[override]
            """List files by pattern. Pure logic in tools.glob."""
            from .ga_tools import glob as _glob
            blob = _glob.handle(self.cwd, args)
            yield blob[:400] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_grep(self, args, response):  # type: ignore[override]
            """Search content via ripgrep. Pure logic in tools.grep."""
            from .ga_tools import grep as _grep
            blob = _grep.handle(self.cwd, args)
            yield blob[:400] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_batch(self, args, response):  # type: ignore[override]
            """Fan out read-side tools in parallel via ga_tools.batch.

            Sub-calls are dispatched via a closure that mirrors the
            single-tool yields, returning the final string blob each
            tool produced.
            """
            from .ga_tools import batch as _batch
            from .ga_tools import (
                glob as _glob, grep as _grep, web_search as _ws,
            )
            from .ga_tools.read_tracker import ReadTracker

            reader = getattr(self, "_aiforge_reader", None)
            if reader is None:
                reader = ReadTracker()
                self._aiforge_reader = reader  # type: ignore[attr-defined]

            def _dispatch(tool_name: str, sub_args: dict) -> str:
                if tool_name == "glob":
                    return _glob.handle(self.cwd, sub_args)
                if tool_name == "grep":
                    return _grep.handle(self.cwd, sub_args)
                if tool_name == "file_read":
                    abs_path = self._get_abs_path(sub_args.get("path", ""))
                    return reader.read(abs_path)
                if tool_name == "web_search":
                    return _ws.handle(sub_args)
                if tool_name == "ask_explorer":
                    q = (sub_args.get("question") or "").strip()
                    if not q:
                        return "[ask_explorer] empty question"
                    return self._spawn_one_explorer(q)
                return f"[batch] unknown tool {tool_name!r}"

            calls = args.get("calls") or []
            yield f"[batch] {len(calls)} sub-call(s) parallel\n"
            blob = _batch.handle(_dispatch, calls)
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_bulk_edit(self, args, response):  # type: ignore[override]
            """Apply N file_patch edits atomically — Aider-style.

            Iterates ``edits`` calling super().do_file_patch one
            at a time; on the first failure, rolls back every file
            already touched in this batch via ``git checkout HEAD
            -- <path>``. Counters bump per landed edit (mirrors
            single-call file_patch semantics).
            """
            if getattr(self, "_plan_mode_reject_next", False):
                self._plan_mode_reject_next = False
                from .ga_tools import plan_mode as _pm
                yield ""
                return StepOutcome(
                    {"status": "error", "msg": "blocked by plan_mode"},
                    next_prompt=_pm.reject_message("bulk_edit"),
                )
            from .ga_tools import bulk_edit as _bulk
            edits = args.get("edits") or []

            def _apply_one(edit: dict) -> dict:
                gen = self.do_file_patch({
                    "path": edit["path"],
                    "old_content": edit["old_content"],
                    "new_content": edit["new_content"],
                }, response)
                outcome = None
                try:
                    while True:
                        next(gen)
                except StopIteration as e:
                    outcome = e.value
                if outcome is None or outcome.data is None:
                    return {"status": "error", "msg": "no outcome"}
                if isinstance(outcome.data, dict):
                    return outcome.data
                return {"status": "success", "msg": str(outcome.data)[:200]}

            blob = _bulk.handle(self.cwd, edits, _apply_one)
            yield blob[:600] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob, next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_java_refactor(self, args, response):  # type: ignore[override]
            """Run an OpenRewrite recipe via mvn rewrite:run."""
            if getattr(self, "_plan_mode_reject_next", False):
                self._plan_mode_reject_next = False
                from .ga_tools import plan_mode as _pm
                yield ""
                return StepOutcome(
                    {"status": "error", "msg": "blocked by plan_mode"},
                    next_prompt=_pm.reject_message("java_refactor"),
                )
            from .ga_tools import java_refactor as _jr
            blob = _jr.handle(self.cwd, args)
            yield blob[:600] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob, next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_lint(self, args, response):  # type: ignore[override]
            from .ga_tools import lint as _lint
            blob = _lint.handle(self.cwd, args)
            yield blob[:600] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob, next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_tests(self, args, response):  # type: ignore[override]
            from .ga_tools import tests as _tests
            blob = _tests.handle(self.cwd, args)
            yield blob[:600] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob, next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_undo(self, args, response):  # type: ignore[override]
            from .ga_tools import undo as _undo
            blob = _undo.handle(self.cwd, args)
            yield blob[:400] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob, next_prompt=self._get_anchor_prompt(skip=False),
            )

        def do_bash(self, args, response):  # type: ignore[override]
            """Persistent bash session. State held on handler._aiforge_shell."""
            from .ga_tools import bash as _bash
            shell = getattr(self, "_aiforge_shell", None)
            if shell is None:
                shell = _bash.PersistentShell(cwd=self.cwd)
                self._aiforge_shell = shell  # type: ignore[attr-defined]
            blob = _bash.handle(shell, args)
            yield blob[:400] + ("\n" if not blob.endswith("\n") else "")
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        # ─── Plan Mode (Claude-Code-style think-before-edit) ─────
        def do_enter_plan_mode(self, args, response):  # type: ignore[override]
            from .ga_tools import plan_mode as _pm
            blob = _pm.enter(self, args.get("reason") or "")
            yield blob + "\n"
            return StepOutcome(
                blob,
                next_prompt=("Plan Mode is active. Use read-only tools "
                             "(file_read / glob / grep / ask_explorer) to "
                             "investigate. Call exit_plan_mode(plan=...) "
                             "to unlock writes."),
            )

        def do_exit_plan_mode(self, args, response):  # type: ignore[override]
            from .ga_tools import plan_mode as _pm
            blob = _pm.exit_(self, args.get("plan") or "")
            yield blob + "\n"
            return StepOutcome(
                blob,
                next_prompt=self._get_anchor_prompt(skip=False),
            )

        # ─── TodoWrite checklist ────────────────────────────────
        def do_todo_write(self, args, response):  # type: ignore[override]
            from .ga_tools import todos as _td
            blob = _td.write(self, args.get("items") or [])
            yield blob + "\n"
            return StepOutcome(
                blob,
                next_prompt="Checklist updated. Continue with the next pending item.",
            )

        def do_todo_check(self, args, response):  # type: ignore[override]
            from .ga_tools import todos as _td
            blob = _td.check(
                self,
                int(args.get("id") or 0),
                str(args.get("status") or "pending"),
            )
            yield blob + "\n"
            return StepOutcome(
                blob,
                next_prompt="Checklist updated. Move to next pending item.",
            )

        # ─── Sub-agent dispatch (isolated context) ──────────────
        def do_dispatch_subagent(self, args, response):  # type: ignore[override]
            from .ga_tools import subagent as _sa
            from .ga_tools import llm_config as _llm_cfg
            ga = import_ga()
            task = (args.get("task") or "").strip()
            if not task:
                yield "[subagent] empty task\n"
                return StepOutcome(
                    {"status": "error", "msg": "task required"},
                    next_prompt="dispatch_subagent needs a task string.",
                )
            # Cap one dispatch per parent turn to prevent runaway fan-out.
            if getattr(self, "_subagent_this_turn", -1) == self.current_turn:
                yield "[subagent] already used this turn — wait one turn\n"
                return StepOutcome(
                    {"status": "error", "msg": "1 dispatch per turn"},
                    next_prompt="dispatch_subagent already fired this turn.",
                )
            self._subagent_this_turn = self.current_turn  # type: ignore[attr-defined]

            class _SubHandler:
                _done_hooks: list = []
                max_turns = 0
                current_turn = 0

                def __getattr__(self, name):
                    raise AttributeError(name)

            answer = _sa.run_subagent(
                task=task,
                parent_cfg=_llm_cfg.primary_cfg(),
                full_tools_schema=tools_schema,
                allowed_tools=args.get("allowed_tools"),
                max_turns=int(args.get("max_turns") or 8),
                spawn_session=lambda c: ga["LLMSession"](cfg=c),
                handler_cls=_SubHandler,
                runner=ga["agent_runner_loop"],
            )
            yield f"[subagent] {len(answer)} chars returned\n"
            return StepOutcome(
                answer,
                next_prompt="Sub-agent answer above. Continue your own task.",
            )

        def _spawn_one_explorer(self, question: str) -> str:
            """Run a single explorer sub-agent; return text answer."""
            import subprocess as _sp
            ga_path = ga_dir()
            sub_id = f"explorer-{int(time.time() * 1000)}-{abs(hash(question)) & 0xfffff:x}"
            sub_dir = os.path.join(ga_path, "temp", sub_id)
            os.makedirs(sub_dir, exist_ok=True)
            try:
                _sp.run(
                    [sys.executable,
                     os.path.join(ga_path, "agentmain.py"),
                     "--task", sub_id, "--bg", "--input", question,
                     "--llm_no", "0", "--verbose=False"],
                    cwd=ga_path,
                    timeout=15,
                    capture_output=True,
                    env={**os.environ, "GA_LANG": "en"},
                )
            except Exception as exc:
                return f"[ask_explorer:{sub_id}] spawn failed: {exc}"
            out_path = os.path.join(sub_dir, "output.txt")
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(5)
                if os.path.exists(out_path):
                    try:
                        text = open(out_path, encoding="utf-8").read()
                    except Exception:
                        text = ""
                    if "[ROUND END]" in text:
                        return text.split("[ROUND END]")[0][-4000:]
            return f"[ask_explorer:{sub_id}] timeout (5 min)"

        def do_ask_explorer(self, args, response):  # type: ignore[override]
            """Spawn one OR many read-only sub-agents in parallel.

            Single mode: ``args.question`` (str).
            Parallel mode: ``args.questions`` (list[str]) — fans out
            up to 4 sub-agents via ThreadPoolExecutor and returns
            joined answers in input order.
            """
            import concurrent.futures as _cf
            single = (args.get("question") or "").strip()
            multi_raw = args.get("questions") or []
            multi = [
                q.strip() for q in multi_raw if isinstance(q, str) and q.strip()
            ]
            if single:
                multi = [single] + multi
            multi = multi[:4]  # cap parallelism
            if not multi:
                yield "[ask_explorer] no question(s) given.\n"
                return StepOutcome(
                    {"status": "error", "msg": "question required"},
                    next_prompt="ask_explorer needs `question` or `questions`.",
                )
            yield f"[ask_explorer] spawning {len(multi)} sub-agent(s)\n"
            if len(multi) == 1:
                answer = self._spawn_one_explorer(multi[0])
                yield f"[ask_explorer] done\n"
                return StepOutcome(
                    {"status": "ok", "answer": answer},
                    next_prompt=None,
                )
            # Parallel mode — bounded by len(multi).
            answers: list[str] = [""] * len(multi)
            with _cf.ThreadPoolExecutor(max_workers=len(multi)) as ex:
                futures = {
                    ex.submit(self._spawn_one_explorer, q): i
                    for i, q in enumerate(multi)
                }
                for fut in _cf.as_completed(futures):
                    answers[futures[fut]] = fut.result()
            joined = "\n\n".join(
                f"=== [{i}] {q[:80]} ===\n{a}"
                for i, (q, a) in enumerate(zip(multi, answers))
            )
            yield f"[ask_explorer] {len(multi)} sub-agents finished\n"
            return StepOutcome(
                {"status": "ok", "answers": answers, "joined": joined[:8000]},
                next_prompt=None,
            )

        def do_file_patch(self, args, response):  # type: ignore[override]
            # Plan Mode write-guard short-circuit.
            if getattr(self, "_plan_mode_reject_next", False):
                self._plan_mode_reject_next = False
                from .ga_tools import plan_mode as _pm
                yield ""
                return StepOutcome(
                    {"status": "error", "msg": "blocked by plan_mode"},
                    next_prompt=_pm.reject_message("file_patch"),
                )
            outcome_gen = super().do_file_patch(args, response)
            outcome = yield from outcome_gen
            success = (
                isinstance(outcome.data, dict)
                and outcome.data.get("status") in ("success", "ok")
            )
            if success:
                self._counters["edit_block_ok"] = (
                    self._counters.get("edit_block_ok", 0) + 1
                )
                # Append edit-verify diff so the model sees the
                # actual change that landed (Claude Edit-style).
                from .ga_tools import edit_verify as _ev
                abs_path = self._get_abs_path(args.get("path", ""))
                verify = _ev.banner_for(abs_path, self.cwd)
                if verify:
                    yield verify[:1500] + (
                        "\n" if not verify.endswith("\n") else ""
                    )
                # Post-edit hooks (.aiforge/hooks.yml). Errors logged
                # to next_prompt only when block:true + non-zero exit.
                self._run_event_hooks("post_edit", outcome)
            return outcome

        def do_file_write(self, args, response):  # type: ignore[override]
            if getattr(self, "_plan_mode_reject_next", False):
                self._plan_mode_reject_next = False
                from .ga_tools import plan_mode as _pm
                yield ""
                return StepOutcome(
                    {"status": "error", "msg": "blocked by plan_mode"},
                    next_prompt=_pm.reject_message("file_write"),
                )
            outcome_gen = super().do_file_write(args, response)
            outcome = yield from outcome_gen
            if isinstance(outcome.data, dict) and outcome.data.get("status") == "success":
                self._counters["edit_block_ok"] = (
                    self._counters.get("edit_block_ok", 0) + 1
                )
                self._run_event_hooks("post_edit", outcome)
            return outcome

        # ─── Ops-MCP dynamic dispatch ───────────────────────────
        def __getattr__(self, name):
            """Route do_ops_<server>_<tool> → mcp_http.call_tool.

            GenericAgentHandler defines do_<concrete_tool> at class
            level; dynamic dispatch only fires for tool names not
            otherwise resolved (Python attribute lookup order). KISS:
            we only handle the ``do_ops_`` namespace here so we never
            shadow GA's stock methods.
            """
            if not name.startswith("do_ops_"):
                raise AttributeError(name)
            ops_map = self.__dict__.get("_ops_name_map") or {}
            tname = name[3:]
            if tname not in ops_map:
                raise AttributeError(name)
            url, raw_tool = ops_map[tname]
            handler_self = self

            def _gen(args, response):
                from aiforge_core.runtime.mcp_http import call_tool as _ops_call
                clean = {k: v for k, v in args.items() if k != "_index"}
                yield f"[ops_mcp] {raw_tool}({clean})\n"
                result = _ops_call(url, raw_tool, clean)
                return StepOutcome(
                    result[:6000],
                    next_prompt=handler_self._get_anchor_prompt(skip=False),
                )
            return _gen

        # ─── Post-event hooks dispatcher (Claude-Code-style) ────
        def _run_event_hooks(self, event: str, outcome) -> None:
            """Run .aiforge/hooks.yml entries matching ``event``.

            Best-effort: errors logged via outcome.next_prompt suffix
            when block=true. Disabled unless AIFORGE_DOER_HOOKS=1.
            """
            if os.environ.get("AIFORGE_DOER_HOOKS", "0") != "1":
                return
            cached = getattr(self, "_aiforge_hooks_cfg", None)
            if cached is None:
                from .ga_tools import hooks as _hk
                cached = _hk.load(self.cwd)
                self._aiforge_hooks_cfg = cached  # type: ignore[attr-defined]
            if not cached:
                return
            from .ga_tools import hooks as _hk
            results = _hk.run_for_event(cached, event, cwd=self.cwd)
            blob = _hk.render(results)
            if blob:
                # Append summary to outcome.next_prompt so the model
                # sees what fired without changing data shape.
                blocked = _hk.first_blocked(results)
                if blocked is not None:
                    suffix = (
                        f"\n\n{blob}\n\n[hooks] BLOCK — fix the failing "
                        f"hook before continuing."
                    )
                else:
                    suffix = f"\n\n{blob}"
                if outcome.next_prompt is not None:
                    outcome.next_prompt = outcome.next_prompt + suffix

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
                    self._run_event_hooks("post_compile", outcome)
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

    # Allowlist + scope guard for write tools. Resolution priority:
    #   1. body's '## Allowed files' block (planner-written, explicit)
    #   2. ticket.metadata.enrichment.focal_files (IntentLayer, repo-
    #      scoped, splitter-vetted edit_targets)
    # Was: body only — when the planner crashed (e.g. LM Studio
    # disconnected), allowed=∅ → doer flew blind → 0 edits → blocked.
    body = getattr(ticket, "body", "") or ""
    allowed = parse_allowed_files(body)
    if not allowed:
        md = getattr(ticket, "metadata", None) or {}
        enr = md.get("enrichment") if isinstance(md, dict) else None
        if isinstance(enr, dict):
            focal = enr.get("focal_files") or []
            # CRITICAL: focal_files come from IntentLayer as absolute
            # paths under the MASTER worktree. The doer runs in a
            # per-ticket sibling worktree. We MUST translate every
            # path so ScopeGuard accepts only the worktree copy AND
            # the system prompt shows the worktree path (otherwise
            # the model writes to the master tree → no commit on the
            # feature branch → publish skipped → master leak).
            for p in focal[:6]:
                if not isinstance(p, str):
                    continue
                resolved: str | None = None
                if os.path.isabs(p):
                    # Translate master abs → worktree abs by stripping
                    # the AIFORGE_REPOS_BASE/<project>/ prefix.
                    base_repo = os.path.dirname(worktree_path.rstrip("/"))
                    # worktree_path = .../X/.aiforge-worktrees/ONE-N
                    # base_repo    = .../X/.aiforge-worktrees
                    # We need .../X — go up one more.
                    repo_root = os.path.dirname(base_repo)
                    if repo_root and p.startswith(repo_root + "/"):
                        rel = p[len(repo_root) + 1:]
                        cand = os.path.join(worktree_path, rel)
                        if os.path.isfile(cand):
                            resolved = cand
                    if resolved is None and os.path.isfile(p):
                        # Path is abs but not under repo_root — last
                        # resort, allow as-is (rare edge case).
                        resolved = p
                else:
                    cand = os.path.join(worktree_path, p)
                    if os.path.isfile(cand):
                        resolved = cand
                if resolved:
                    allowed.add(resolved)
    scope_guard = ScopeGuard(allowed)

    # GA needs a per-task scratch dir for parent.task_dir reads (_keyinfo, _intervene).
    task_dir = os.path.join(_ga_path, "temp", f"aiforge-{identifier}-{int(t_start)}")
    os.makedirs(task_dir, exist_ok=True)
    parent = _ParentShim(task_dir=task_dir)

    counters = {"edit_block_ok": 0, "compile_green": 0}
    # Stash the ticket on the GA parent so do_ask_user can post the
    # question back as a ticket event instead of blocking on stdin.
    parent._aiforge_ticket = ticket  # type: ignore[attr-defined]
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

    # All backends use LLMSession (GA's text-protocol path). ToolClient
    # is wired for text-protocol — passing tools array via system
    # prompt and reading `<tool_use>{...}</tool_use>` blocks from the
    # model's response. Both mlx-lm 0.31 (which drops native tool_calls)
    # and cloud APIs (Gemini/OpenAI) follow the text format reliably
    # when their system prompt instructs them to.
    from .ga_tools import llm_config as _llm_cfg
    cfg = _llm_cfg.primary_cfg()
    session = LLMSession(cfg=cfg)
    emit(log, "ga_runner.session_built", ticket=identifier,
         backend=cfg.get("name", "?"),
         class_=session.__class__.__name__)
    # Provider-aware prompt-cache markers (Anthropic ephemeral / OpenAI
    # prefix-cache). Best-effort, idempotent.
    try:
        from aiforge_core.llm import cache_markers as _cm
        _provider_name = cfg.get("name", "").split("-")[0] or "openai"
        _cm.apply_to_session(session, provider=_provider_name, role="doer")
    except Exception as _exc:
        emit(log, "ga_runner.cache_markers_skipped",
             ticket=identifier, err=str(_exc)[:200])
    client = ToolClient(session)

    # Auto-compaction (AIFORGE_DOER_COMPACT=1). Wraps session.raw_ask
    # so we trim middle history middle-out before each LLM round-trip
    # whenever estimated tokens cross 0.8 * context_win. KISS: no LLM
    # summary call — single [SUMMARY] line elision marker.
    if os.environ.get("AIFORGE_DOER_COMPACT", "0") == "1":
        from .ga_tools import compaction as _cmp
        _orig_raw_ask = session.raw_ask

        def _compacting_raw_ask(messages, *a, **kw):
            new_history, did = _cmp.maybe_compact(
                session.history, session.context_win,
                threshold=float(os.environ.get(
                    "AIFORGE_DOER_COMPACT_THRESHOLD", "0.8")),
                keep_head=int(os.environ.get(
                    "AIFORGE_DOER_COMPACT_KEEP_HEAD", "2")),
                keep_tail=int(os.environ.get(
                    "AIFORGE_DOER_COMPACT_KEEP_TAIL", "6")),
            )
            if did:
                session.history = new_history
                emit(log, "ga_runner.compacted",
                     ticket=identifier,
                     msg_count=len(new_history))
            # raw_ask returns a generator — pass it through verbatim.
            return _orig_raw_ask(messages, *a, **kw)

        session.raw_ask = _compacting_raw_ask  # type: ignore[assignment]

    tools_schema = _load_tools_schema()
    # Inject ask_explorer (custom tool, not in GA's stock schema).
    if os.environ.get("AIFORGE_DOER_ASK_EXPLORER", "1") == "1":
        tools_schema = list(tools_schema) + [_ASK_EXPLORER_SCHEMA]
    # Inject web_search backed by Gemini grounded search. Only enable
    # when AIFORGE_GOOGLE_API_KEY is present in the environment so
    # offline / no-key deployments don't advertise a tool that 401s.
    from .ga_tools.web_search import SCHEMA as _WS_SCHEMA
    from .ga_tools.glob import SCHEMA as _GLOB_SCHEMA
    from .ga_tools.grep import SCHEMA as _GREP_SCHEMA
    from .ga_tools.bash import SCHEMA as _BASH_SCHEMA
    from .ga_tools.batch import SCHEMA as _BATCH_SCHEMA
    from .ga_tools.bulk_edit import SCHEMA as _BULK_EDIT_SCHEMA
    from .ga_tools.java_refactor import SCHEMA as _JR_SCHEMA
    from .ga_tools.lint import SCHEMA as _LINT_SCHEMA
    from .ga_tools.tests import SCHEMA as _TESTS_SCHEMA
    from .ga_tools.undo import SCHEMA as _UNDO_SCHEMA
    from .ga_tools.plan_mode import (
        SCHEMA_ENTER as _PM_ENTER, SCHEMA_EXIT as _PM_EXIT,
    )
    from .ga_tools.todos import (
        SCHEMA_WRITE as _TODO_WRITE, SCHEMA_CHECK as _TODO_CHECK,
    )
    from .ga_tools.subagent import SCHEMA as _SUBAGENT_SCHEMA
    if os.environ.get("AIFORGE_GOOGLE_API_KEY"):
        tools_schema = list(tools_schema) + [_WS_SCHEMA]
    # Always add local utilities — no API key needed.
    tools_schema = list(tools_schema) + [
        _GLOB_SCHEMA, _GREP_SCHEMA, _BASH_SCHEMA, _BATCH_SCHEMA,
        _BULK_EDIT_SCHEMA, _JR_SCHEMA,
        _LINT_SCHEMA, _TESTS_SCHEMA, _UNDO_SCHEMA,
    ]
    # Phase-A KISS gaps: each behind its own env flag so we can
    # bisect regressions per feature.
    if os.environ.get("AIFORGE_DOER_PLAN_MODE", "0") == "1":
        tools_schema = list(tools_schema) + [_PM_ENTER, _PM_EXIT]
    if os.environ.get("AIFORGE_DOER_TODOS", "0") == "1":
        tools_schema = list(tools_schema) + [_TODO_WRITE, _TODO_CHECK]
    if os.environ.get("AIFORGE_DOER_SUBAGENT", "0") == "1":
        tools_schema = list(tools_schema) + [_SUBAGENT_SCHEMA]

    # Ops MCP tools (mongo / k8s / tekton / tally) — Doer can probe
    # SA / DB state when fixing tickets. Discovered once per process;
    # toggle via AIFORGE_DOER_OPS_MCP=1.
    ops_name_map: dict = {}
    if os.environ.get("AIFORGE_DOER_OPS_MCP", "0") == "1":
        try:
            from aiforge_core.runtime.mcp_http import (
                all_tools_with_origin, render_schema_for_openai,
            )
            discovered = all_tools_with_origin()
            ops_schemas, ops_name_map = render_schema_for_openai(
                discovered, prefix="ops_",
            )
            if ops_schemas:
                tools_schema = list(tools_schema) + ops_schemas
                emit(log, "ga_runner.ops_mcp_loaded",
                     ticket=identifier, count=len(ops_schemas))
        except Exception as exc:
            emit(log, "ga_runner.ops_mcp_failed",
                 ticket=identifier, err=str(exc)[:200])
    handler._ops_name_map = ops_name_map  # type: ignore[attr-defined]
    # Per-repo defaults from .aiforge/aiforge.conf.yml (lift to env).
    try:
        from .ga_tools import repo_config as _rc
        _rc.apply_to_env(_rc.load(worktree_path))
    except Exception:
        pass
    # Centralised standards catalogue (Neo4j :Repo + worktree YAML)
    # — single source for build/test/lint/format/security commands.
    # Lifted to env so legacy ga_tools that read AIFORGE_*_CMD pick
    # them up without code change. apply_to_env() preserves existing
    # env values so operator overrides still win.
    try:
        from aiforge_core.runtime import repo_standards as _rs
        _repo_name = os.path.basename(os.path.normpath(worktree_path))
        _std = _rs.get(_repo_name, worktree=worktree_path)
        _rs.apply_to_env(_std)
        emit(log, "ga_runner.standards_loaded",
             ticket=identifier, repo=_repo_name, source=_std.source)
    except Exception as exc:
        emit(log, "ga_runner.standards_failed",
             ticket=identifier, err=str(exc)[:200])
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
            s = str(chunk)
            chunks.append(s)
            # GA emits "LLM Running (Turn N) ..." at the top of every turn.
            m = re.search(r"LLM Running \(Turn (\d+)\)", s)
            if m:
                turn_count = max(turn_count, int(m.group(1)))
            # Diagnostic — surface every turn boundary + tool dispatch
            # + early-exit signals so we can see WHY GA stops at turn 1.
            # Toggle off via AIFORGE_DEBUG_DOER=0.
            if os.environ.get("AIFORGE_DEBUG_DOER", "1") == "1":
                if "未知工具" in s or "Tool:" in s or "RESULT" in s:
                    emit(log, "ga_runner.chunk", ticket=identifier,
                         turn=turn_count, head=s[:200])
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
