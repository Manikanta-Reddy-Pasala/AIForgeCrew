"""Production ticket processor — ADK 2.x SequentialAgent over the v6 pipeline.

Polls Postgres for a `todo` ticket via :func:`tickets.store.claim_next_any`,
drives one run of the v6 pipeline:

    SequentialAgent[
        planner,
        verifier,
        LoopAgent[doer, feedback]   # cap = 3 iterations
        learner,
    ]

then maps the final session state to a ticket status and exits. systemd
``Restart=always RestartSec=10`` keeps the loop polling.

Provider routing per archetype is read from
:func:`aiforge_core.config.agent_config.resolve_litellm`. For
``claude_local`` (subscription CLI) the LiteLLM wrapper cannot subprocess,
so the runner logs a warning and skips that archetype back to ``local``.
Wiring claude_local through ADK is a follow-up — see TODO at bottom.

Invoke:
    python -m aiforge_core.runtime.adk_runner
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from aiforge_core.agents.loader import load_agents
from aiforge_core.config import agent_config as _acfg
from aiforge_core.tickets import store as tickets_mod

log = logging.getLogger("adk_runner")
logging.basicConfig(
    level=os.environ.get("AIFORGE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _build_litellm_model(role: str):
    """Return an ADK BaseLlm for the given role.

    Routing:
      * ``claude_local`` → ``ClaudeSubscriptionLlm`` (subprocess CLI,
        subscription auth via OAuth keychain; no API billing).
      * everything else → ``LiteLlm`` (LiteLLM-backed standard path).
    """
    from google.adk.models.lite_llm import LiteLlm
    cfg = _acfg.resolve_litellm(role)
    if cfg.get("_claude_cli"):
        from .claude_subscription_llm import ClaudeSubscriptionLlm
        # ClaudeSubscriptionLlm reads AIFORGE_CLAUDE_BIN / _HOST from env.
        # Strip the litellm `anthropic/` prefix if present — CLI takes a
        # bare model id.
        model_id = cfg["model_id"]
        if model_id.startswith("anthropic/"):
            model_id = model_id.split("/", 1)[1]
        return ClaudeSubscriptionLlm(model=model_id)
    kwargs: dict[str, Any] = {"model": cfg["model_id"]}
    if cfg.get("api_base"):
        kwargs["api_base"] = cfg["api_base"]
    if cfg.get("api_key"):
        kwargs["api_key"] = cfg["api_key"]
    return LiteLlm(**kwargs)


def _build_pipeline():
    """Construct the v6 SequentialAgent. Tool wiring is deferred — Doer
    runs without external tools for now (file_write/file_patch will be
    added in a follow-up; see TODO)."""
    from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent

    contracts = load_agents()  # parses agents.yaml, validates v6 shape

    def _agent(role: str, instruction: str, output_key: str,
               tools: list | None = None) -> "LlmAgent":
        c = contracts[role]
        kwargs: dict[str, Any] = {
            "name": role,
            "model": _build_litellm_model(role),
            "instruction": instruction,
            "output_key": output_key,
            "timeout": c.contract.max_wall_s,
        }
        if tools:
            kwargs["tools"] = tools
        return LlmAgent(**kwargs)

    planner = _agent(
        "planner",
        instruction=(
            "You are the AIForge Planner. Read the parent ticket and emit a "
            "JSON plan with {steps, scope_allowlist_globs, child_subtickets}. "
            "Every test subticket MUST reference a test skeleton template."
        ),
        output_key="plan_md",
    )
    verifier = _agent(
        "verifier",
        instruction=(
            "You are the plan verifier. Critique the plan in state['plan_md']. "
            "Return STRICT JSON only: "
            "{verdict: pass|reject, issues: [...], rationale: <one-line>}. "
            "Reject if any subticket has empty scope_allowlist_globs, a step "
            "targets a missing file/symbol, or no test subticket exists."
        ),
        output_key="verifier_verdict",
    )
    from .doer_tools import adk_function_tools as _doer_tools
    doer = _agent(
        "doer",
        instruction=(
            "You are the Doer. Execute the plan in state['plan_md'] by "
            "calling tools — DO NOT reply with prose narrating what you "
            "would do. The available tools are file_read, file_write, "
            "file_patch (find/replace one occurrence), list_dir, and "
            "run_shell.\n"
            "\n"
            "Workflow per subticket:\n"
            "  1. list_dir / file_read to inspect the target file.\n"
            "  2. file_write or file_patch to make the edit.\n"
            "  3. run_shell to compile / run tests when applicable.\n"
            "\n"
            "When the change is in place, return STRICT JSON: "
            "{file_diffs: [{path, action: write|patch}], "
            "compile_status: green|red|skipped, "
            "test_status: green|red|skipped, "
            "turn_log: <one-line summary>}.\n"
            "\n"
            "Stay inside the subticket's scope_allowlist_globs. Refuse to "
            "call file_write on any path outside that allowlist."
        ),
        output_key="doer_outcome",
        tools=_doer_tools(),
    )
    feedback = _agent(
        "feedback",
        instruction=(
            "You are the post-execution judge. Inspect state['doer_outcome'] "
            "and return STRICT JSON: "
            "{verdict: pass|fail|scope_violation, rationale: <evidence>}. "
            "scope_violation outranks fail."
        ),
        output_key="feedback_verdict",
    )
    learner = _agent(
        "learner",
        instruction=(
            "You are the Learner. ONLY when state['feedback_verdict'].verdict "
            "== 'pass', emit JSON facts_json: "
            "[{text, about: [path|fqn|ticket], tags}]. Otherwise emit []."
        ),
        output_key="facts_json",
    )

    doer_loop = LoopAgent(
        name="doer_feedback_loop",
        sub_agents=[doer, feedback],
        max_iterations=3,
    )
    return SequentialAgent(
        name="aiforge_v6_pipeline",
        sub_agents=[planner, verifier, doer_loop, learner],
    )


def _fetch_memory_block(ticket) -> str:
    """Pre-flight AiForgeMemory recall for the claimed ticket.

    Pulls hits from the unified retrieval surface (memory hybrid search +
    ticket brief + related_memories + sym_lookup + find_doc) and formats
    them as a markdown block. Empty string when recall returns nothing
    or the memory backend is unreachable — never raises.
    """
    try:
        from aiforge_core.memory import unified_query as _uq
        text = f"{ticket.title}\n{ticket.body or ''}"
        result = _uq.query(text, ticket=ticket.identifier, limit=8)
    except Exception as exc:
        log.warning("memory recall failed: %s", exc)
        return ""
    hits = result.get("hits") or []
    if not hits:
        return ""
    lines = ["## Memory hits (AiForgeMemory)", ""]
    for h in hits[:8]:
        src = h.get("source", "?")
        score = h.get("score", 0.0)
        body = (h.get("text") or h.get("body") or h.get("summary") or "")[:300]
        lines.append(f"- [{src} {score:.2f}] {body}")
    sources = ",".join(result.get("used_sources") or [])
    log.info("memory: %d hits sources=%s", len(hits), sources)
    return "\n".join(lines) + "\n"


def _process_one_ticket() -> bool:
    """Claim + run a single ticket. Returns True when one ran, False when
    the queue was empty (caller exits and lets systemd back off)."""
    ticket = tickets_mod.claim_next_any()
    if ticket is None:
        return False

    log.info("claimed ticket=%s title=%r", ticket.identifier, ticket.title)
    memory_block = _fetch_memory_block(ticket)
    pipeline = _build_pipeline()

    try:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types as gtypes
        import asyncio

        session_svc = InMemorySessionService()
        runner = Runner(
            agent=pipeline, app_name="aiforge",
            session_service=session_svc, auto_create_session=True,
        )
        prompt = (
            f"# Ticket {ticket.identifier}\n"
            f"## Title\n{ticket.title}\n\n"
            f"## Body\n{ticket.body or '(no body)'}\n"
        )
        if memory_block:
            prompt += "\n" + memory_block

        async def _run() -> dict:
            session = await session_svc.create_session(
                app_name="aiforge", user_id="aiforge-runner",
            )
            content = gtypes.Content(
                role="user", parts=[gtypes.Part.from_text(text=prompt)],
            )
            final_state: dict = {}
            async for event in runner.run_async(
                user_id="aiforge-runner",
                session_id=session.id, new_message=content,
            ):
                if event.is_final_response():
                    final_state = dict(session.state) if session.state else {}
            # Re-fetch session for the post-run state snapshot
            session = await session_svc.get_session(
                app_name="aiforge", user_id="aiforge-runner",
                session_id=session.id,
            )
            return dict(session.state or {})

        state = asyncio.run(_run())
        verdict = (state.get("feedback_verdict") or {})
        if isinstance(verdict, str):
            try:
                verdict = json.loads(verdict)
            except json.JSONDecodeError:
                verdict = {}
        outcome = verdict.get("verdict", "fail") if isinstance(verdict, dict) else "fail"

        # Map verdict to a tickets-store-valid status:
        #   pass             → done
        #   scope_violation  → cancelled (clear operator signal)
        #   fail / anything  → blocked (needs human triage)
        new_status = {
            "pass": "done",
            "scope_violation": "cancelled",
        }.get(outcome, "blocked")
        tickets_mod.update_status(
            ticket.id, new_status, role="adk_runner",
            metadata_patch={
                "feedback_verdict": outcome,
                "verifier_verdict": (state.get("verifier_verdict") or {}).get("verdict")
                                    if isinstance(state.get("verifier_verdict"), dict)
                                    else None,
            },
        )
        log.info("ticket=%s status=%s verdict=%s",
                 ticket.identifier, new_status, outcome)
    except Exception as exc:
        log.exception("ticket=%s failed during ADK run: %s", ticket.identifier, exc)
        try:
            tickets_mod.update_status(
                ticket.id, "blocked", role="adk_runner",
                metadata_patch={"error": str(exc)[:500]},
            )
        except Exception:
            pass
    return True


def main() -> int:
    """Single-shot: claim one ticket, run it, exit. systemd re-polls."""
    if _process_one_ticket():
        return 0
    # Empty queue — sleep briefly so systemd back-off doesn't hammer logs.
    time.sleep(int(os.environ.get("AIFORGE_POLL_IDLE_S", "10")))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# TODO follow-ups (out of scope for this commit):
# 1. Wrap claude_local CLI in a custom ``google.adk.models.BaseLlm`` so the
#    subscription path participates in ADK runs (not just LiteLLM).
# 2. Wire the Doer's tools (file_write, file_patch, code_run) via ADK
#    FunctionTool — currently the Doer runs without filesystem tools.
# 3. Honour the per-agent `forbidden` list from agents.yaml at the ADK
#    callback layer (defense-in-depth — schema filter is just one layer).
# 4. Replace InMemorySessionService with the persistent one once we want
#    multi-pod coordination; today single-pod systemd is fine.
