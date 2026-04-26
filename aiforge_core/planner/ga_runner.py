"""GenericAgent text-protocol adapter for the Planner role.

Mirrors ``aiforge_core.doer.ga_runner`` but targets the planner model
(``Qwen3.6-27B-UD-MLX-4bit`` on ``http://127.0.0.1:1235/v1``) and a
planner-specific tool schema. Used when ``AIFORGE_PLANNER_BACKEND=
genericagent`` (or when ``agents.yaml`` declares
``backend: genericagent_text_protocol`` for the planner role).

Why GA for the planner:
- mlx_lm 0.31 native ``tool_calls`` serialization bug applies to BOTH
  models on MS. Smolagents CodeAgent times out at 1200s on the 27B
  model; GA's text-protocol session sidesteps the bug.
- Planner needs to read repos + write a plan markdown — a small subset
  of GA's atomic tools covers it: ``file_read`` + ``code_run`` (for
  grep/find/lookup) + ``file_write`` (for the plan).

The plan is persisted to Postgres via ``aiforge_core.runtime.tickets.
update_plan`` after the GA loop returns. The model writes the plan
content into ``<task_dir>/plan.md`` via GA's ``file_write`` tool; the
adapter reads it back and pushes to the ticket.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.logging_setup import emit


_FORBIDDEN_GA_TOOLS = {
    "ask_user",
    "start_long_term_update",
    "web_scan",
    "web_execute_js",
}


def _ga_dir() -> str:
    p = os.environ.get("AIFORGE_GA_DIR", "")
    if p and os.path.isdir(p):
        return p
    for cand in (
        "/home/mani/genericagent",
        "/Users/manikanta/genericagent",
        os.path.expanduser("~/genericagent"),
    ):
        if os.path.isdir(cand):
            return cand
    raise RuntimeError(
        "GenericAgent dir not found; set AIFORGE_GA_DIR to override"
    )


def _planner_llm_config() -> dict:
    """Text-protocol cfg for GA's LLMSession on the planner.

    Honours global AIFORGE_PRIMARY_BACKEND so a Settings flip
    swaps every agent at once. Falls through to per-role
    AIFORGE_PLANNER_* vars when backend=local.
    """
    from aiforge_core.runtime.llm_picker import pick as _pick
    ep = _pick("planner")
    if ep.backend == "gemini":
        base_url = ep.base_url.rstrip("/").rstrip("/v1")
        model = ep.model
        api_key = ep.api_key
    else:
        base_url = os.environ.get(
            "AIFORGE_PLANNER_BASE_URL", "http://127.0.0.1:1235"
        )
        model = os.environ.get(
            "AIFORGE_PLANNER_MODEL",
            "/Users/manikanta/.lmstudio/models/unsloth/Qwen3.6-27B-UD-MLX-4bit",
        )
        api_key = os.environ.get("AIFORGE_PLANNER_API_KEY", "sk-local")
    cfg: dict = {
        "name": ("gemini-planner" if ep.backend == "gemini"
                 else "mlx-planner"),
        "apikey": api_key,
        "apibase": base_url.rstrip("/").rstrip("/v1"),
        "model": model,
        "api_mode": "chat_completions",
        "max_retries": 2,
        "connect_timeout": 15,
        "read_timeout": int(os.environ.get("AIFORGE_PLANNER_READ_TIMEOUT", "600")),
        "context_win": int(os.environ.get("AIFORGE_PLANNER_CTX", "60000")),
        "max_tokens": int(os.environ.get("AIFORGE_PLANNER_MAX_TOKENS", "8192")),
        "temperature": float(os.environ.get("AIFORGE_PLANNER_TEMP", "0.2")),
    }
    if os.environ.get("AIFORGE_PLANNER_TOP_P"):
        cfg["top_p"] = float(os.environ["AIFORGE_PLANNER_TOP_P"])
    if os.environ.get("AIFORGE_PLANNER_THINK", "0") == "1":
        cfg["chat_template_kwargs"] = {"enable_thinking": True}
    return cfg


def _load_tools_schema(ga_dir: str) -> list[dict]:
    schema_path = Path(ga_dir) / "assets" / "tools_schema.json"
    raw = json.loads(schema_path.read_text())
    keep = {"file_read", "code_run", "file_write", "update_working_checkpoint"}
    return [
        t for t in raw
        if t.get("function", {}).get("name") in keep
        and t.get("function", {}).get("name") not in _FORBIDDEN_GA_TOOLS
    ]


def _build_planner_prompt(ticket: object, repo_root: str) -> str:
    body = getattr(ticket, "body", "") or ""
    title = getattr(ticket, "title", "") or ""
    project = getattr(ticket, "project", "") or ""
    return (
        f"# Planner task: {title}\n\n"
        f"## Project\n{project}\n\n"
        f"## Working dir\n{repo_root}\n\n"
        f"## Ticket body\n{body}\n\n"
        f"## Your job\n"
        f"Read enough of the codebase to write a concise plan for the doer. "
        f"The plan must include:\n"
        f"- ## Goal: 1-2 lines restating the objective\n"
        f"- ## Files: bullet list of file paths the doer will edit\n"
        f"- ## Steps: numbered list of edits\n"
        f"- ## Acceptance criteria: copy from the ticket body\n\n"
        f"## Tools\n"
        f"- file_read <path>: read a file\n"
        f"- code_run <bash>: run a shell command (rg, find, ls, cat) "
        f"  starting with `cd {repo_root} &&`\n"
        f"- file_write <path>: write the final plan to "
        f"  {repo_root}/.aiforge/plan.md when ready\n\n"
        f"## Done condition\n"
        f"After file_write succeeds for the plan, end the run with no further "
        f"tool calls. Do not call ask_user. Do not write code edits — that "
        f"is the doer's job. Keep the plan under 2KB."
    )


def _persist_plan(ticket: object, plan_text: str, log: object | None) -> None:
    """Persist the plan as a ticket event so it lives in Postgres
    and shows up in the API. The doer also reads the plan from the
    worktree's `.aiforge/plan.md` directly, so persistence is best-
    effort — failures don't block the run."""
    ticket_id = getattr(ticket, "id", None)
    if ticket_id is None:
        return
    try:
        tickets_mod.add_event(
            ticket_id, "planner", "plan_written",
            plan_text[:8000],
        )
        emit(log, "ga_planner.plan_persisted",
             ticket_id=ticket_id, chars=len(plan_text))
    except Exception as exc:
        emit(log, "ga_planner.persist_failed",
             ticket_id=ticket_id, error=str(exc)[:200])


def run_planner_via_ga(ticket: object, log: object | None = None) -> dict:
    """Run the Planner as a direct one-shot LLM call.

    Despite the function name (kept for API stability with the
    orchestrator), this implementation does NOT spin up GA's full
    agent loop. F-suite analysis showed GA's text-protocol tool schema
    + Chinese boilerplate adds 2.7KB to every turn — wasted on a
    role that just needs to read the ticket and emit a markdown plan.

    A single requests.post to the OpenAI-compatible chat completions
    endpoint with a slim system+user prompt produces the plan in <1
    minute on qwen-coder-next vs 10+ minutes on full GA loops.

    Returns the same ``{stop_reason, summary, wall_s, backend}`` shape
    as ``run_planner`` (smolagents) for orchestrator compatibility.
    """
    import requests

    t_start = time.time()
    identifier = getattr(ticket, "identifier", "?")
    project = getattr(ticket, "project", "") or "PosClientBackend"
    cfg = _planner_llm_config()
    base_url = cfg["apibase"].rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    repo_root = os.path.join(
        os.environ.get("AIFORGE_REPOS_BASE", "/home/mani/codeRepo"),
        project,
    )
    plan_dir = os.path.join(repo_root, ".aiforge")
    os.makedirs(plan_dir, exist_ok=True)
    plan_path = os.path.join(plan_dir, "plan.md")

    system_prompt = (
        "You are the AIForge planner. Read the ticket and emit a "
        "concise markdown plan for the doer to follow.\n\n"
        "Output ONLY the markdown plan — no preface, no fences, no "
        "explanation.\n\n"
        "The plan MUST follow this exact shape so GenericAgent's plan-"
        "mode can track it:\n\n"
        "## Goal\n<1-2 lines>\n\n"
        "## Files\n- <repo-relative path>\n\n"
        "## Steps\n"
        "- [ ] step 1: concrete action with file path\n"
        "- [ ] step 2: ...\n"
        "- [ ] step N: run mvn -DskipTests compile from worktree\n\n"
        "## Acceptance criteria\n<copy verbatim from ticket body>\n\n"
        "Each step must be a single line starting with `- [ ]`. The "
        "doer marks steps `[x]` as it completes them; plan-mode auto-"
        "exits when all steps are checked. Keep total under 1500 "
        "characters."
    )
    body = getattr(ticket, "body", "") or ""
    title = getattr(ticket, "title", "") or ""
    user_prompt = (
        f"# Ticket: {title}\n\n"
        f"Project: {project}\n"
        f"Worktree: {repo_root}\n\n"
        f"## Ticket body (verbatim)\n{body}\n\n"
        f"## Output\nReturn the four-section markdown plan only."
    )

    emit(log, "ga_planner.start", ticket=identifier,
         repo=repo_root, plan_path=plan_path,
         mode="direct_litellm",
         system_chars=len(system_prompt),
         user_chars=len(user_prompt))

    try:
        # Token budget: when thinking is enabled, the model spends a
        # large chunk on reasoning before emitting any plan content.
        # Old cap of 1500 starved the response → empty plan.
        thinking_on = bool(cfg.get("chat_template_kwargs", {}).get(
            "enable_thinking"
        ))
        budget = cfg.get("max_tokens", 8192)
        if not thinking_on:
            budget = min(budget, 1500)
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": budget,
            "temperature": cfg.get("temperature", 0.2),
        }
        if cfg.get("chat_template_kwargs"):
            body["chat_template_kwargs"] = cfg["chat_template_kwargs"]
        if cfg.get("top_p"):
            body["top_p"] = cfg["top_p"]
        resp = requests.post(
            f"{base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {cfg.get('apikey','sk-local')}"},
            timeout=cfg.get("read_timeout", 600),
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        # Different stacks expose the post-thinking text via different
        # fields. mlx_lm's gemma stack uses 'reasoning' for the trace +
        # 'content' for the answer. Qwen3.x with --enable-thinking uses
        # 'reasoning_content'. LM Studio passes through whatever the
        # underlying chat template emits. Prefer 'content' first, fall
        # back so we never lose the answer when the field name shifts.
        plan_text = (msg.get("content") or "").strip()
        if not plan_text:
            plan_text = (msg.get("reasoning_content") or "").strip()
        if not plan_text:
            # Strip <think>…</think> wrappers that some templates emit
            # inline in 'content' when thinking-mode is on.
            raw = (msg.get("reasoning") or "").strip()
            if raw:
                import re as _re
                cleaned = _re.sub(r"<think>.*?</think>", "", raw,
                                  flags=_re.DOTALL).strip()
                plan_text = cleaned or raw
    except Exception as exc:
        emit(log, "ga_planner.exception", ticket=identifier,
             error=str(exc)[:300])
        return {
            "stop_reason": "exception",
            "summary": f"planner LLM call failed: {exc}",
            "wall_s": round(time.time() - t_start, 2),
            "backend": "direct_litellm",
        }

    if plan_text:
        try:
            Path(plan_path).write_text(plan_text)
        except Exception as exc:
            emit(log, "ga_planner.plan_write_failed",
                 ticket=identifier, error=str(exc)[:200])
        _persist_plan(ticket, plan_text, log)

    wall_s = round(time.time() - t_start, 2)
    emit(log, "ga_planner.done", ticket=identifier, wall_s=wall_s,
         plan_chars=len(plan_text), mode="direct_litellm")

    return {
        "stop_reason": "done" if plan_text else "no_plan",
        "summary": (
            f"plan ({len(plan_text)} chars) written to {plan_path}"
            if plan_text else "planner produced empty response"
        ),
        "wall_s": wall_s,
        "backend": "direct_litellm",
        "plan_text": plan_text,
    }


def _legacy_run_planner_via_ga_loop(ticket: object, log: object | None = None) -> dict:
    """Old GA-driven planner — kept as reference. Not used in production
    after F-suite analysis showed direct LiteLLM is 10x faster for the
    plan-emission task. Can be re-enabled via AIFORGE_PLANNER_GA_LOOP=1
    if a future fixture genuinely needs tool calls in the planner.
    """
    t_start = time.time()
    identifier = getattr(ticket, "identifier", "?")
    project = getattr(ticket, "project", "") or "PosClientBackend"

    ga_dir = _ga_dir()
    sys.path.insert(0, ga_dir)
    try:
        from agent_loop import agent_runner_loop, exhaust  # type: ignore
        from llmcore import LLMSession, ToolClient  # type: ignore
        from ga import GenericAgentHandler  # type: ignore
    except Exception as exc:
        emit(log, "ga_planner.import_failed", ticket=identifier,
             error=str(exc)[:200])
        return {
            "stop_reason": "exception",
            "summary": f"GA import failed: {exc}",
            "wall_s": round(time.time() - t_start, 2),
            "backend": "genericagent",
        }

    repo_root = os.path.join(
        os.environ.get("AIFORGE_REPOS_BASE", "/home/mani/codeRepo"),
        project,
    )
    if not os.path.isdir(repo_root):
        emit(log, "ga_planner.repo_missing", ticket=identifier, repo=repo_root)
        return {
            "stop_reason": "exception",
            "summary": f"repo not found: {repo_root}",
            "wall_s": round(time.time() - t_start, 2),
            "backend": "genericagent",
        }

    plan_dir = os.path.join(repo_root, ".aiforge")
    os.makedirs(plan_dir, exist_ok=True)
    plan_path = os.path.join(plan_dir, "plan.md")
    if os.path.exists(plan_path):
        os.remove(plan_path)

    cfg = _planner_llm_config()
    session = LLMSession(cfg=cfg)
    client = ToolClient(session)
    tools_schema = _load_tools_schema(ga_dir)

    task_dir = os.path.join(
        ga_dir, "temp",
        f"aiforge-planner-{identifier}-{int(t_start)}",
    )
    os.makedirs(task_dir, exist_ok=True)

    class _ParentShim:
        def __init__(self, td: str) -> None:
            self.task_dir = td
            self.verbose = False
            self._turn_end_hooks: dict = {}

    parent = _ParentShim(task_dir)
    handler = GenericAgentHandler(parent, [], repo_root)

    system_prompt = (
        "You are the AIForge planner. Read the codebase and produce a "
        "concise markdown plan for the doer to follow. You do not edit "
        "production code. End the run after writing the plan to the path "
        "the user gives you. Do not call ask_user."
    )
    user_input = _build_planner_prompt(ticket, repo_root)

    max_turns = int(os.environ.get("AIFORGE_PLANNER_MAX_TURNS", "20"))
    emit(log, "ga_planner.start", ticket=identifier,
         max_turns=max_turns, repo=repo_root, plan_path=plan_path)

    try:
        gen = agent_runner_loop(
            client, system_prompt, user_input,
            handler, tools_schema,
            max_turns=max_turns, verbose=False,
        )
        result = exhaust(gen)
    except Exception as exc:
        emit(log, "ga_planner.exception", ticket=identifier,
             error=str(exc)[:300])
        return {
            "stop_reason": "exception",
            "summary": f"GA loop failed: {exc}",
            "wall_s": round(time.time() - t_start, 2),
            "backend": "genericagent",
        }

    plan_text = ""
    if os.path.exists(plan_path):
        try:
            plan_text = Path(plan_path).read_text()
        except Exception:
            plan_text = ""
    if plan_text:
        _persist_plan(ticket, plan_text, log)

    wall_s = round(time.time() - t_start, 2)
    emit(log, "ga_planner.done", ticket=identifier,
         wall_s=wall_s, plan_chars=len(plan_text),
         loop_result=str(result)[:120])

    summary = (
        f"GA planner wrote {len(plan_text)} chars to {plan_path}"
        if plan_text else "GA planner produced no plan"
    )
    return {
        "stop_reason": "done" if plan_text else "no_plan",
        "summary": summary,
        "wall_s": wall_s,
        "backend": "genericagent",
        "plan_text": plan_text,
    }
