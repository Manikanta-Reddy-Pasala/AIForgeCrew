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


_DOER_GA_PREAMBLE = """You are the AIForge Doer agent operating through GenericAgent.

You MUST modify code so the ticket is implemented. You only get credit when:
  1. At least one file_patch or file_write call against an allowed file.
  2. `mvn -DskipTests compile` exits 0 inside the worktree.
  3. Every Acceptance bullet's identifier appears in the new file content.

Hard rules:
- Edit ONLY files listed in the ## Allowed files section. Writes outside that list
  are blocked by the harness ScopeGuard.
- Do NOT call `ask_user`. Do NOT call `start_long_term_update`. Do NOT call any
  web tool. The harness will reject those calls.
- Use file_patch for narrow diffs (preferred). Use file_write only for new files
  or full rewrites.
- After each edit, run `mvn -DskipTests compile` via code_run inside the
  worktree. If compile fails, read the error and make ONE more file_patch.
- Do NOT call final-answer / no_tool until compile is green. Re-edit and recompile.

Work in the provided worktree path. Every code_run command must `cd <worktree>` first.

When the task is complete (compile green + acceptance verified), reply with a
single message containing a `<summary>` tag that names the modified file and
quotes the BUILD SUCCESS line, then stop calling tools.
"""


def _build_user_input(ticket: object, plan_text: str, worktree_path: str,
                      allowed: set[str]) -> str:
    body = getattr(ticket, "body", "") or ""
    title = getattr(ticket, "title", "") or ""
    allowed_block = (
        "\n".join(f"- {p}" for p in sorted(allowed))
        if allowed else "(no scope constraint)"
    )
    return (
        f"## Worktree\n`{worktree_path}` — every command must run there.\n\n"
        f"## Ticket\n{title}\n\n"
        f"{body}\n\n"
        f"## Allowed files (write-tool ScopeGuard)\n{allowed_block}\n\n"
        f"## Planner notes\n{plan_text or '(none)'}\n\n"
        f"## REQUIRED workflow — DO NOT SKIP STEPS\n"
        f"1. file_read EACH file under '## Allowed files' (absolute path "
        f"   under `{worktree_path}`).\n"
        f"2. file_patch the change required by the acceptance criteria. "
        f"   You MUST emit at least one successful file_patch (or "
        f"   file_write) call before finishing — the harness rejects "
        f"   runs with edit_block_ok=0.\n"
        f"3. code_run `cd {worktree_path} && mvn -DskipTests compile` "
        f"   only AFTER the patch lands.\n"
        f"4. End with a single `<summary>` tag that names the modified "
        f"   file and quotes the BUILD line.\n\n"
        f"Running mvn before editing is a NO-OP — the original code "
        f"already compiles. The objective is to ADD the change "
        f"specified in the acceptance criteria."
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
    return {
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

    cfg = _doer_llm_config()
    session = LLMSession(cfg=cfg)
    client = ToolClient(session)

    tools_schema = _load_tools_schema()
    user_input = _build_user_input(ticket, plan_text, worktree_path, allowed)
    chunks: list[str] = []
    turn_count = 0

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

    # Commit / push are handled by run_smolagents_doer's _git_commit_push_pr
    # in the original orchestrator path. We borrow that helper rather than
    # duplicating it.
    try:
        from .orchestrator_bridge import _git_commit_push_pr
        pub = _git_commit_push_pr(
            ticket, worktree_path, final_summary, changed_files, log,
        )
    except Exception as exc:
        emit(log, "ga_runner.publish_failed", ticket=identifier, err=str(exc)[:200])
        pub = {"commit_sha": None, "pushed": False, "pr_url": None}

    if ticket_id is not None:
        comment_body = (final_summary or "(no summary)")[:3500]
        if pub.get("pr_url"):
            comment_body = f"{comment_body}\n\nPR: {pub['pr_url']}"
        elif pub.get("commit_sha"):
            comment_body = f"{comment_body}\n\nCommit: {pub['commit_sha']}"
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
