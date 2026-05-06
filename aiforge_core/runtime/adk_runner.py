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
    """Return an ADK BaseLlm for the given role, with cloud escalation.

    Wraps the role's primary model (mlx-lm via LiteLlm or
    ClaudeSubscriptionLlm via the subscription CLI) inside
    :class:`EscalatingLlm`, which transparently retries on Ollama Cloud
    → Anthropic → Claude subscription when the primary errors out or
    returns garbage. Hard-disable the chain with
    ``AIFORGE_ESCALATE_DISABLE=1`` (or per-role via the same env on the
    cloud_escalation_chain helper).
    """
    from .escalating_llm import EscalatingLlm
    primary = _acfg.resolve_litellm(role)
    chain = _acfg.cloud_escalation_chain(role)
    return EscalatingLlm.build(role, primary, chain)


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
            "would do. Available tools: file_read, file_write, file_patch "
            "(find/replace one occurrence), list_dir, run_shell, "
            "memory_lookup (search AiForgeMemory for symbols/concepts you "
            "don't recognise).\n"
            "\n"
            "Anti-hallucination protocol:\n"
            "  - Before importing or referencing any class/function not in "
            "    the file you're editing, call memory_lookup or list_dir + "
            "    file_read to confirm it exists.\n"
            "  - file_write rejects content with unbalanced braces / "
            "    Python-style kwargs in Java / unparseable Python. If you "
            "    get back {ok: False, error: 'syntax_invalid: ...'}, fix "
            "    the syntax and try again — never paste the same draft.\n"
            "  - On any tool error, read the error string and adjust. "
            "    Do NOT loop the same call. If you've tried twice without "
            "    progress, return verdict=fail with the blocker.\n"
            "\n"
            "Workflow per subticket:\n"
            "  1. list_dir / file_read to inspect the target file.\n"
            "  2. memory_lookup if you need symbol/import context.\n"
            "  3. file_write or file_patch to make the edit.\n"
            "  4. run_shell to compile / run tests when applicable.\n"
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


def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a git/gh command, capture stdout/stderr. Caller decides on
    return-code semantics. 5-min hard timeout per call."""
    import subprocess
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "")[:1000], (proc.stderr or "")[:1000]


def _commit_push_open_pr(ticket) -> dict:
    """Commit Doer edits, push to origin, open PR via gh CLI.

    Branch name comes from ``ticket.branch`` (already populated by
    ``_derive_branch`` at ticket-create time). Skips the PR step
    cleanly when:
      * working tree clean (Doer didn't actually write — return empty)
      * gh CLI absent or unauthenticated (push still happens, PR is
        skipped with a logged hint)

    Returns a metadata patch dict with `pr_url`, `branch_pushed`, and
    optional `pr_skip_reason`. Empty dict on hard failure.
    """
    import shutil

    repo_root = os.path.expanduser(os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))
    if not os.path.isdir(os.path.join(repo_root, ".git")):
        log.warning("git_pr.skip: %s is not a git repo", repo_root)
        return {"pr_skip_reason": "not_a_git_repo"}

    rc, out, err = _run_git(["git", "status", "--porcelain"], repo_root)
    if rc != 0:
        log.warning("git_pr.status_failed: %s", err)
        return {"pr_skip_reason": "git_status_failed"}
    if not out.strip():
        log.info("git_pr.clean: no changes to commit")
        return {"pr_skip_reason": "no_changes"}

    branch = ticket.branch or f"aiforge/{ticket.identifier}"
    # Re-create branch from current HEAD so retries don't conflict.
    _run_git(["git", "branch", "-D", branch], repo_root)  # ignore rc
    rc, out, err = _run_git(["git", "checkout", "-b", branch], repo_root)
    if rc != 0:
        log.warning("git_pr.checkout_failed: %s", err)
        return {"pr_skip_reason": "checkout_failed"}

    _run_git(["git", "add", "-A"], repo_root)
    title = (ticket.title or ticket.identifier).strip().replace("\n", " ")
    msg = f"feat({ticket.identifier}): {title}\n\nGenerated by AIForgeCrew v6 pipeline."
    rc, out, err = _run_git(["git", "commit", "-m", msg], repo_root)
    if rc != 0 and "nothing to commit" not in (out + err):
        log.warning("git_pr.commit_failed: %s", err)
        return {"pr_skip_reason": "commit_failed"}

    rc, out, err = _run_git(
        ["git", "push", "-u", "origin", branch], repo_root,
    )
    if rc != 0:
        log.warning("git_pr.push_failed: %s", err)
        return {"branch_pushed": False, "pr_skip_reason": "push_failed",
                "push_err": err[:300]}

    if not shutil.which("gh"):
        log.info("git_pr.gh_absent — push done, skipping PR creation")
        return {"branch_pushed": True, "pr_skip_reason": "gh_not_installed"}

    pr_body = (
        f"AIForgeCrew v6 pipeline auto-generated PR for ticket "
        f"{ticket.identifier}.\n\n"
        f"## Original ticket body\n{(ticket.body or '')[:1500]}"
    )
    rc, out, err = _run_git(
        ["gh", "pr", "create",
         "--title", f"{ticket.identifier}: {title}",
         "--body", pr_body],
        repo_root,
    )
    if rc != 0:
        log.warning("git_pr.gh_create_failed: %s", err)
        return {"branch_pushed": True, "pr_skip_reason": "gh_create_failed",
                "gh_err": err[:300]}

    pr_url = (out or "").strip().splitlines()[-1] if out else ""
    log.info("git_pr.opened: %s", pr_url)
    return {"branch_pushed": True, "pr_url": pr_url}


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

        # PR gate: anything that ISN'T an explicit scope_violation is
        # eligible. Reason — Feedback judge is brittle (often emits
        # text instead of strict JSON, parser falls back to `fail`),
        # but if the Doer actually wrote files on disk, that's real
        # evidence of work that deserves a PR for human review.
        # `_commit_push_open_pr` itself short-circuits when the working
        # tree is clean, so verdict=fail with no edits remains a no-op.
        pr_meta: dict[str, Any] = {}
        if outcome != "scope_violation":
            pr_meta = _commit_push_open_pr(ticket)
        tickets_mod.update_status(
            ticket.id, new_status, role="adk_runner",
            metadata_patch={
                "feedback_verdict": outcome,
                "verifier_verdict": (state.get("verifier_verdict") or {}).get("verdict")
                                    if isinstance(state.get("verifier_verdict"), dict)
                                    else None,
                **pr_meta,
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
