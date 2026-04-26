"""Google ADK workflow replacing the LangGraph orchestrator.

This module wires the existing AIForgeCrew agents (planner, doer,
feedback, learner) into a Google ADK ``SequentialAgent`` containing a
``LoopAgent`` for the doer/feedback retry cycle. Each AIForge role is
expressed as a thin ``BaseAgent`` subclass whose ``_run_async_impl``
delegates to the production implementation that LangGraph used to call:

    * planner — ``aiforge_core.planner.runner.run_planner``
    * doer    — ``aiforge_core.doer.orchestrator_bridge.run_smolagents_doer``
                (which already routes to GenericAgent when
                ``AIFORGE_DOER_BACKEND=genericagent``)
    * feedback — single-shot LiteLLM call (port of ``feedback_node``)
    * learner — single-shot LiteLLM call + Neo4j ``:Fact`` write
                (port of ``learner_node``)

Cross-cutting concerns are handled by ``Neo4jMirrorPlugin`` which mirrors
every ADK ``Event`` to a ``:Turn`` node linked to the ticket's
``:Session`` node.

The Phase 11 cleanup will delete ``aiforge_core/graph/*`` once this
runner is the only production path. Until then, both modules co-exist —
this file does NOT import any langgraph symbols.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.plugins import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.genai import types as genai_types

from aiforge_core.agents import AgentContract, load_agents
from aiforge_core.runtime import tickets as tickets_mod
from aiforge_core.runtime.config import (
    AIFORGE_DSN,
    FEEDBACK_MODEL,
    LEARNER_MODEL,
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
)
from aiforge_core.runtime.logging_setup import emit, get_logger


# ─────────────────────────── Module constants ───────────────────────────
# Mirrors aiforge_core.graph.edges.MAX_FEEDBACK_FAILS — the LoopAgent's
# max_iterations is the number of (doer, feedback) pairs allowed before
# the feedback verdict is forced to fail terminal.
MAX_FEEDBACK_FAILS = 4

# State keys the agents read/write through ``ctx.session.state``. Keep
# these short — DatabaseSessionService persists them as JSON in Postgres.
S_TICKET_ID = "aiforge.ticket_id"
S_WORKTREE = "aiforge.worktree_path"
S_VERDICT = "aiforge.verdict"
S_FIXLIST = "aiforge.feedback_fixlist"
S_FAIL_COUNT = "aiforge.feedback_fail_count"
S_COMPILE_FAIL_COUNT = "aiforge.compile_fail_count"
S_PLAN_DONE = "aiforge.plan_done"
S_LAST_DOER_SUMMARY = "aiforge.last_doer_summary"


# ─────────────────────────── Plugin ─────────────────────────────────────
class Neo4jMirrorPlugin(BasePlugin):
    """Mirror every ADK Event onto Neo4j as a ``:Turn`` node linked to
    the ticket's ``:Session`` node.

    Fail-soft: a Neo4j outage must not break the workflow.
    Reuses the legacy driver from ``aiforge_core.legacy.rag.neo4j_memory``
    so we don't multiply connection pools.
    """

    name: str = "neo4j_mirror"

    def __init__(self) -> None:
        super().__init__(name=self.name)
        self._log = get_logger("adk.neo4j_mirror")

    async def on_event_callback(
        self,
        *,
        invocation_context: InvocationContext,
        event: Event,
    ) -> Event | None:
        try:
            from aiforge_core.legacy.rag.neo4j_memory import _get_driver
            ticket_id = invocation_context.session.state.get(S_TICKET_ID)
            session_id = invocation_context.session.id
            payload = {
                "session_id": session_id,
                "ticket_id": ticket_id,
                "author": event.author,
                "invocation_id": event.invocation_id,
                "event_id": event.id,
                "timestamp": event.timestamp,
                "partial": bool(event.partial),
                "summary": _event_text_summary(event)[:1500],
            }
            cy = (
                "MERGE (s:Session {id: $session_id}) "
                "ON CREATE SET s.created_at = timestamp(), s.ticket_id = $ticket_id "
                "CREATE (t:Turn {"
                "  event_id: $event_id, author: $author, "
                "  invocation_id: $invocation_id, summary: $summary, "
                "  ts: $timestamp, partial: $partial"
                "}) "
                "MERGE (s)-[:HAS_TURN]->(t)"
            )
            drv = _get_driver()
            with drv.session() as s:
                s.run(cy, **payload)
        except Exception as exc:
            emit(self._log, "neo4j_mirror.error", error=str(exc)[:200])
        return None  # never suppress the event


def _event_text_summary(event: Event) -> str:
    """Best-effort one-line summary of an ADK Event."""
    if event.content is not None and getattr(event.content, "parts", None):
        chunks: list[str] = []
        for p in event.content.parts:
            txt = getattr(p, "text", None)
            if txt:
                chunks.append(txt)
        if chunks:
            return " ".join(chunks).replace("\n", " ")
    return ""


# ─────────────────────────── Helpers ────────────────────────────────────
def _yield_text_event(author: str, text: str, invocation_id: str) -> Event:
    """Build an ADK Event whose content is a single text part."""
    return Event(
        invocation_id=invocation_id,
        author=author,
        content=genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=text)],
        ),
    )


def _load_contract(role: str) -> AgentContract | None:
    try:
        return load_agents().get(role)
    except Exception:
        return None


def _ticket_from_state(state: dict) -> tickets_mod.Ticket | None:
    tid = state.get(S_TICKET_ID)
    if tid is None:
        return None
    return tickets_mod.get(int(tid))


# Markers that indicate a ticket SHOULD result in a curl-able HTTP
# endpoint. When any of these appear in title or body, the workflow
# auto-enables the IntegrationTestAgent and the feedback gate demands
# test_green ≥ 1 — closing the ONE-7-style hole where the doer commits
# only a repository method and skips the controller.
_ENDPOINT_TICKET_MARKERS = (
    "@getmapping", "@postmapping", "@putmapping", "@deletemapping",
    "@requestmapping", "endpoint reachable",
    "get /v1/", "get /api/", "post /v1/", "post /api/",
    "expose a new get endpoint", "expose a new post endpoint",
    "new api", "new endpoint", "rest endpoint",
    "/v1/api/", "controller method",
)


def _emit_stage_event(
    ticket_id: int,
    stage: str,
    t_start: float,
    extra: dict | None = None,
) -> None:
    """Write a per-stage timing breadcrumb into ticket events.

    The ticket UI / API renders events chronologically — these give
    operators a wall-clock view of where each ticket spent its time
    (planner / doer turns / integration smoke / feedback verdict /
    publish push / learner fact write).
    """
    duration_s = round(time.time() - t_start, 2)
    extra = dict(extra or {})
    body = f"{stage} done in {duration_s}s"
    if extra:
        body += " | " + ", ".join(
            f"{k}={v}" for k, v in extra.items() if v is not None
        )
    try:
        tickets_mod.add_event(
            ticket_id, stage, "stage_done",
            body=body[:1000],
            metadata={"stage": stage, "duration_s": duration_s, **extra},
        )
    except Exception:
        # Telemetry is best-effort; never fail the agent on logging.
        pass


def _ticket_needs_endpoint_smoke(ticket: tickets_mod.Ticket) -> bool:
    """True when ticket text describes endpoint creation/modification.

    Conservative — false positives just add a smoke step (cheap), false
    negatives let bad doer commits through (expensive). Markers chosen
    to match how product/PM phrases new-API tickets.
    """
    haystack = " ".join([
        (ticket.title or ""),
        (ticket.body or ""),
    ]).lower()
    return any(m in haystack for m in _ENDPOINT_TICKET_MARKERS)


# ─────────────────────────── Planner agent ──────────────────────────────
class AiForgePlannerAgent(BaseAgent):
    """ADK wrapper around the existing smolagents Planner.

    Reads ``ticket_id`` from ``ctx.session.state`` and runs the smolagents
    CodeAgent inside a worker thread (smolagents.run is sync). Yields one
    text Event with the planner's summary.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.planner")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[planner] no ticket in state",
                ctx.invocation_id,
            )
            return

        contract = _load_contract("planner")
        max_wall_s = contract.contract.max_wall_s if contract else 600

        emit(log, "adk.planner.start",
             ticket=ticket.identifier, max_wall_s=max_wall_s,
             backend=os.environ.get("AIFORGE_PLANNER_BACKEND", "smolagents"))

        # Backend dispatch: env > agents.yaml > smolagents default.
        backend = os.environ.get("AIFORGE_PLANNER_BACKEND")
        if not backend and contract is not None:
            decl = (contract.identity.backend or "").lower()
            if decl in ("genericagent_text_protocol", "genericagent"):
                backend = "genericagent"
        if backend == "genericagent":
            from aiforge_core.planner.ga_runner import run_planner_via_ga
            run_planner = run_planner_via_ga
        else:
            from aiforge_core.planner import run_planner

        loop = asyncio.get_event_loop()
        t_start = time.time()
        try:
            summary = await asyncio.wait_for(
                loop.run_in_executor(None, run_planner, ticket, log),
                timeout=max_wall_s,
            )
        except asyncio.TimeoutError:
            emit(log, "adk.planner.timeout", ticket=ticket.identifier,
                 wall_s=round(time.time() - t_start, 2))
            yield _yield_text_event(
                self.name,
                f"[planner] timeout after {max_wall_s}s",
                ctx.invocation_id,
            )
            return
        except Exception as exc:
            emit(log, "adk.planner.exception",
                 ticket=ticket.identifier, error=str(exc)[:300])
            yield _yield_text_event(
                self.name,
                f"[planner] exception: {exc}",
                ctx.invocation_id,
            )
            return

        state[S_PLAN_DONE] = True
        emit(log, "adk.planner.done",
             ticket=ticket.identifier,
             stop_reason=summary.get("stop_reason"),
             wall_s=summary.get("wall_s"))
        _emit_stage_event(
            ticket.id, "planner", t_stage_start,
            extra={
                "stop_reason": summary.get("stop_reason"),
                "plan_chars": summary.get("plan_chars"),
            },
        )

        text = summary.get("summary") or "(planner ran)"
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=text[:4000])],
            ),
            actions=EventActions(
                state_delta={S_PLAN_DONE: True},
            ),
        )


# ─────────────────────────── Doer agent ─────────────────────────────────
class AiForgeDoerAgent(BaseAgent):
    """ADK wrapper around the doer bridge.

    Routes via ``run_smolagents_doer`` — which dispatches to GenericAgent
    when ``AIFORGE_DOER_BACKEND=genericagent`` (the production setting on
    the NUC). Honours ``feedback_fixlist`` from prior loop iterations.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.doer")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[doer] no ticket in state",
                ctx.invocation_id,
            )
            return

        from aiforge_core.runtime.orchestrator import _ensure_branch_and_worktree
        worktree = state.get(S_WORKTREE) or _ensure_branch_and_worktree(ticket)
        if not worktree:
            emit(log, "adk.doer.no_worktree", ticket=ticket.identifier)
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text="[doer] no worktree available")],
                ),
                actions=EventActions(
                    escalate=True,
                    state_delta={S_VERDICT: "blocked"},
                ),
            )
            return

        prior_verdict = state.get(S_VERDICT)
        prior_fixlist = state.get(S_FIXLIST) or ""

        contract = _load_contract("doer")
        max_wall_s = contract.contract.max_wall_s if contract else 700

        emit(log, "adk.doer.start", ticket=ticket.identifier,
             worktree=worktree, prior_verdict=prior_verdict)

        from aiforge_core.doer import run_smolagents_doer

        loop = asyncio.get_event_loop()
        try:
            summary = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: run_smolagents_doer(
                        ticket, worktree, log,
                        prior_verdict=prior_verdict,
                        prior_fixlist=prior_fixlist or None,
                    ),
                ),
                timeout=max_wall_s,
            )
        except asyncio.TimeoutError:
            emit(log, "adk.doer.timeout", ticket=ticket.identifier)
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=f"[doer] timeout after {max_wall_s}s")],
                ),
                actions=EventActions(
                    escalate=True,
                    state_delta={S_VERDICT: "fail"},
                ),
            )
            return
        except Exception as exc:
            emit(log, "adk.doer.exception",
                 ticket=ticket.identifier, error=str(exc)[:300])
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=f"[doer] exception: {exc}")],
                ),
                actions=EventActions(
                    escalate=True,
                    state_delta={S_VERDICT: "fail"},
                ),
            )
            return

        compile_fail_count = state.get(S_COMPILE_FAIL_COUNT, 0)
        stop_reason = summary.get("stop_reason", "")
        if "compile" in stop_reason or stop_reason == "scope_violation":
            compile_fail_count += 1

        # Record the changed files so the publish agent can build a
        # PR description without reaching back into git.
        changed_files: list[str] = []
        if worktree and summary.get("commit_sha"):
            try:
                from aiforge_core.doer.orchestrator_bridge import _run as _bridge_run
                rc, out, _ = _bridge_run(
                    ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                    cwd=worktree, timeout=15,
                )
                if rc == 0:
                    changed_files = [p.strip() for p in out.splitlines() if p.strip()]
            except Exception:
                changed_files = []

        state_delta: dict[str, Any] = {
            S_WORKTREE: worktree,
            S_LAST_DOER_SUMMARY: summary.get("summary", "")[:2000],
            S_COMPILE_FAIL_COUNT: compile_fail_count,
            # Counters are the deterministic feedback gate's input.
            "aiforge.doer.counters": summary.get("counters") or {},
            "aiforge.doer.commit_sha": summary.get("commit_sha"),
            "aiforge.doer.pr_url": summary.get("pr_url"),
            "aiforge.doer.changed_files": changed_files,
        }

        text = summary.get("summary") or f"[doer] stop_reason={stop_reason}"

        actions = EventActions(state_delta=state_delta)
        # If compile_fail_count tripped the cap or the doer hit
        # scope_violation, terminate the loop.
        if compile_fail_count >= 2 or stop_reason == "scope_violation":
            state_delta[S_VERDICT] = (
                "scope_violation" if stop_reason == "scope_violation" else "fail"
            )
            actions = EventActions(escalate=True, state_delta=state_delta)
            emit(log, "adk.doer.escalate", ticket=ticket.identifier,
                 stop_reason=stop_reason,
                 compile_fail_count=compile_fail_count)

        emit(log, "adk.doer.done", ticket=ticket.identifier,
             stop_reason=stop_reason, turns=summary.get("turns"),
             wall_s=summary.get("wall_s"))

        # Continuous-learning: distill outcome → T3 fact (best-effort).
        try:
            from aiforge_core.runtime.doer_learner import distill as _distill
            _distill(ticket, {
                **(summary.get("counters") or {}),
                "stop_reason": stop_reason,
                "summary": summary.get("summary") or "",
                "files_touched": list(changed_files or []),
            })
        except Exception as exc:
            emit(log, "adk.doer.learner_failed",
                 ticket=ticket.identifier, err=str(exc)[:200])
        _emit_stage_event(
            ticket.id, "doer", t_stage_start,
            extra={
                "stop_reason": stop_reason,
                "turns": summary.get("turns"),
                "edits": (summary.get("counters") or {}).get("edit_block_ok"),
                "compile_green": (summary.get("counters") or {}).get("compile_green"),
                "files_changed": len(changed_files),
                "commit_sha": summary.get("commit_sha"),
            },
        )

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=text[:4000])],
            ),
            actions=actions,
        )


# ─────────────────────────── Feedback agent ─────────────────────────────
def _git_diff(worktree_path: str | None) -> str:
    """Return the diff the doer produced. Mirrors the helper in
    ``aiforge_core.graph.nodes.feedback`` so we keep the exact behaviour
    LangGraph had."""
    if not worktree_path:
        return ""
    import subprocess
    try:
        base_proc = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=10, check=False,
        )
        base_ref = "origin/master"
        if base_proc.returncode == 0:
            ref = base_proc.stdout.strip()
            if "/" in ref:
                base_ref = "origin/" + ref.rsplit("/", 1)[1]
        exclude_pathspecs = [
            ":(exclude).flattened-pom.xml",
            ":(exclude,glob)**/.flattened-pom.xml",
            ":(exclude,glob)**/target/**",
            ":(exclude,glob)**/__pycache__/**",
            ":(exclude,glob)**/*.pyc",
            ":(exclude,glob).aiforge-worktrees/**",
        ]
        diffs: list[str] = []
        proc1 = subprocess.run(
            ["git", "diff", f"{base_ref}...HEAD", "--", *exclude_pathspecs],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if proc1.stdout:
            diffs.append(proc1.stdout)
        proc2 = subprocess.run(
            ["git", "diff", "HEAD", "--", *exclude_pathspecs],
            cwd=worktree_path, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if proc2.stdout:
            diffs.append(proc2.stdout)
        combined = "\n".join(diffs) if diffs else "(no diff — Doer made no changes)"
        return combined[:15000]
    except Exception as exc:
        return f"(git diff failed: {exc})"


_FEEDBACK_PROMPT = """You are the Feedback agent. Judge whether the Doer's diff implements the ticket.

## Ticket
{body}

## Diff (git diff origin/main...HEAD)
```
{diff}
```

Rules:
- Respond with a JSON object ONLY. No prose before or after.
- Keys: verdict (one of "pass"|"fail"|"scope_violation"), reason (<= 200 chars), fixlist (optional, array of <= 5 short strings).
- "pass" when the diff implements the acceptance criteria AND compile was green.
- "fail" when the diff misses a criterion or introduces a bug. Populate fixlist with specific asks.
- "scope_violation" when the diff touches files outside the ## Files allowlist.

Your JSON:
"""


def _call_feedback_llm(prompt: str) -> str:
    from aiforge_core.llm import complete as _complete
    return _complete(
        "feedback",
        [{"role": "user", "content": prompt}],
        max_tokens=16384,
        temperature=0.0,
    )


def _parse_verdict(text: str) -> dict:
    if not text:
        return {"verdict": "fail", "reason": "empty verdict", "fixlist": []}
    raw = text.strip()
    if raw.startswith("```"):
        fenced = raw.split("```")
        if len(fenced) >= 3:
            raw = fenced[1].lstrip("json").lstrip("JSON").strip()
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    if isinstance(obj, dict) and "verdict" in obj:
                        obj.setdefault("reason", "")
                        obj.setdefault("fixlist", [])
                        return obj
                except Exception:
                    pass
                start = -1
    import re as _re
    vm = _re.search(r"verdict\s*[:=]\s*\"?(pass|fail|scope_violation)", raw, _re.I)
    if vm:
        verdict = vm.group(1).lower()
        rm = _re.search(r"reason\s*[:=]\s*\"?([^\"\n]{3,300})", raw, _re.I)
        reason = (rm.group(1).strip().rstrip("\",.") if rm else
                  raw[:300].replace("\n", " "))
        return {"verdict": verdict, "reason": reason, "fixlist": []}
    return {"verdict": "fail",
            "reason": f"could not parse verdict JSON (got: {raw[:140]!r})",
            "fixlist": []}


class AiForgeIntegrationTestAgent(BaseAgent):
    """Run unit tests + smoke the new endpoint with real MCP-discovered data.

    Sits between Doer and Feedback in the doer_chain loop. Reads the
    doer's commit, discovers any new @GetMapping endpoint via diff,
    fetches a real ``businessId`` from oneshell-mongo-qa MCP, curls
    the endpoint against AIFORGE_TEST_BASE_URL (default
    http://127.0.0.1:8090), and writes test_green into session state
    for the deterministic feedback gate to consider.

    Gated by AIFORGE_TEST_INTEGRATION=1 so existing flows aren't
    affected when the QA service isn't running.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.integration")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[integration] no ticket in state",
                ctx.invocation_id,
            )
            return
        worktree = state.get(S_WORKTREE)
        if not worktree:
            emit(log, "adk.integration.skipped",
                 ticket=ticket.identifier, reason="no_worktree")
            yield _yield_text_event(
                self.name, "[integration] skipped: no worktree",
                ctx.invocation_id,
            )
            return
        explicit_flag = os.environ.get(
            "AIFORGE_TEST_INTEGRATION", "0"
        ) == "1"
        endpoint_ticket = _ticket_needs_endpoint_smoke(ticket)
        if not (explicit_flag or endpoint_ticket):
            emit(log, "adk.integration.skipped",
                 ticket=ticket.identifier,
                 reason="non_endpoint_ticket")
            yield _yield_text_event(
                self.name,
                "[integration] skipped: not an endpoint ticket",
                ctx.invocation_id,
            )
            return
        from aiforge_core.test.integration_runner import run_integration
        emit(log, "adk.integration.start",
             ticket=ticket.identifier, worktree=worktree)
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, run_integration, worktree, log,
            )
        except Exception as exc:  # noqa: BLE001
            emit(log, "adk.integration.exception",
                 ticket=ticket.identifier, error=str(exc)[:300])
            yield _yield_text_event(
                self.name, f"[integration] exception: {exc}",
                ctx.invocation_id,
            )
            return

        emit(log, "adk.integration.done",
             ticket=ticket.identifier,
             test_green=result.test_green,
             unit_pass=result.unit_tests_pass,
             smoke_pass=result.smoke_pass,
             smoke_status=result.smoke_status_code,
             endpoint=result.smoke_endpoint,
             business_id=result.business_id,
             duration_s=result.duration_s)
        _emit_stage_event(
            ticket.id, "integration", t_stage_start,
            extra={
                "test_green": result.test_green,
                "smoke_status": result.smoke_status_code,
                "endpoint": result.smoke_endpoint or None,
                "business_id": result.business_id or None,
            },
        )

        existing = state.get("aiforge.doer.counters") or {}
        merged = dict(existing)
        merged["test_green"] = 1 if result.test_green else 0
        merged["smoke_status"] = result.smoke_status_code
        state_delta = {
            "aiforge.doer.counters": merged,
            "aiforge.test.endpoint": result.smoke_endpoint,
            "aiforge.test.business_id": result.business_id,
            "aiforge.test.body": result.smoke_body_excerpt,
            "aiforge.test.notes": "; ".join(result.notes)[:400],
        }
        text = (
            f"[integration] test_green={result.test_green} "
            f"smoke={result.smoke_status_code} url={result.smoke_endpoint} "
            f"business_id={result.business_id or '(none)'} "
            f"unit_ran={result.unit_tests_ran}"
        )
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=text)],
            ),
            actions=EventActions(state_delta=state_delta),
        )


class AiForgeFeedbackAgent(BaseAgent):
    """Single-shot LiteLLM verdict on the doer's diff.

    Tools: forbidden=ALL (per agents.yaml). One LLM call per turn.
    Emits ``escalate`` to break the LoopAgent on pass / scope_violation
    or after MAX_FEEDBACK_FAILS consecutive fails.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.feedback")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[feedback] no ticket in state",
                ctx.invocation_id,
            )
            return

        worktree = state.get(S_WORKTREE)
        # ─── Deterministic verdict (gate, not judge) ───────────────────
        # Earlier feedback used an LLM to read the diff blind without
        # being told whether compile passed. It would fail/scope_violation
        # spuriously on perfectly-correct work (ONE-1, ONE-2, ONE-3 all
        # had clean Java + green compile + scope-respected writes but
        # got blocked). Replaced with a deterministic gate:
        #   pass  ⇔ compile_green ≥ 1 AND edit_block_ok ≥ 1 AND no
        #            scope violations from the GA handler
        #   scope_violation ⇔ ScopeGuard rejected ≥ 1 write attempt
        #   fail  ⇔ otherwise (no edits, no compile, or model exited early)
        # Set AIFORGE_FEEDBACK_LLM=1 to fall back to LLM judge.
        counters = state.get("aiforge.doer.counters") or {}
        compile_green = int(counters.get("compile_green", 0) or 0)
        edit_block_ok = int(counters.get("edit_block_ok", 0) or 0)
        scope_violations = int(
            counters.get("scope_violation_count", 0) or 0
        )
        commit_sha = state.get("aiforge.doer.commit_sha")
        diff_for_log = ""
        try:
            diff_for_log = _git_diff(worktree)[:1500]
        except Exception:
            pass

        # When IntegrationTestAgent ran, test_green gates pass too.
        # Required when either:
        #  (a) AIFORGE_TEST_INTEGRATION=1 was set explicitly, OR
        #  (b) the ticket auto-flags as endpoint work (body mentions
        #      "@GetMapping", "GET /…", "endpoint reachable at", etc.)
        # — auto-flag prevents silent doer skips like ONE-7 where the
        # repo method got added but the controller was never wired up.
        test_green_raw = counters.get("test_green")
        endpoint_ticket = _ticket_needs_endpoint_smoke(ticket)
        explicit_flag = os.environ.get(
            "AIFORGE_TEST_INTEGRATION", "0"
        ) == "1"
        test_green_required = (
            (explicit_flag or endpoint_ticket)
            and test_green_raw is not None
        )
        # If the integration agent never even ran but the ticket needs
        # one (endpoint_ticket=True, agent absent), force a fail rather
        # than rubber-stamping. The doer_chain rebuild step ensures the
        # agent is present whenever endpoint_ticket fires; this branch
        # is the safety net for misconfigured runs.
        if endpoint_ticket and test_green_raw is None:
            test_green_required = True
        test_green = int(test_green_raw or 0)
        if scope_violations > 0:
            verdict = "scope_violation"
            reason = (f"ScopeGuard rejected {scope_violations} write(s)")
            fixlist: list[str] = []
        elif edit_block_ok >= 1 and compile_green >= 1 and (
            not test_green_required or test_green >= 1
        ):
            verdict = "pass"
            reason = (
                f"compile_green={compile_green} edit_block_ok={edit_block_ok} "
                f"test_green={test_green} commit={commit_sha or 'none'}"
            )
            fixlist = []
        else:
            verdict = "fail"
            missing: list[str] = []
            if edit_block_ok < 1:
                missing.append("≥1 successful edit")
            if compile_green < 1:
                missing.append("compile-green run")
            if test_green_required and test_green < 1:
                missing.append("integration test green")
            reason = (
                f"edit_block_ok={edit_block_ok} compile_green={compile_green} "
                f"test_green={test_green} — missing: {', '.join(missing)}"
            )
            # Build a targeted fixlist from the doer counters so the next
            # attempt has concrete instructions, not just verdict=fail.
            fixlist = []
            last_compile_error = (counters.get("last_compile_error") or "")[-1500:]
            if edit_block_ok == 0 and compile_green >= 1:
                fixlist.append(
                    "You ran mvn compile but didn't file_patch anything. "
                    "STOP running mvn. file_read every entry under "
                    "## Allowed files, then file_patch the changes."
                )
            if edit_block_ok == 0 and compile_green == 0:
                fixlist.append(
                    "Zero edits, zero compiles. Open each Allowed file "
                    "with file_read, then file_patch the change required "
                    "by acceptance criteria #1. Do this in turn 1-3."
                )
            if "cannot find symbol" in last_compile_error.lower():
                fixlist.append(
                    "Compile error: 'cannot find symbol'. The API you used "
                    "doesn't exist. Use web_search with a query like "
                    "'<framework> <ClassName> Java example' to find the "
                    "right API, or ask_explorer 'show me an example of "
                    "<API> in this repo'. Patch with the correct call."
                )
            if "incompatible types" in last_compile_error.lower():
                fixlist.append(
                    "Compile error: 'incompatible types'. The argument "
                    "types don't match. Re-read the method signature in "
                    "the offending file and adjust your call."
                )
            if "package " in last_compile_error.lower() and "does not exist" in last_compile_error.lower():
                fixlist.append(
                    "Compile error: 'package does not exist'. Wrong import "
                    "path. ask_explorer 'where is <ClassName> defined?' "
                    "and use the actual package."
                )
            if test_green_required and test_green < 1 and edit_block_ok >= 1:
                fixlist.append(
                    "Integration smoke needs a new @GetMapping in the "
                    "controller PLUS the supporting service/repo wiring. "
                    "Verify the controller path matches acceptance #1 "
                    "exactly (no typos in /v1/api/...)."
                )

        # Optional: keep the LLM judge as advisory when explicitly enabled.
        if os.environ.get("AIFORGE_FEEDBACK_LLM") == "1":
            try:
                body = (ticket.body or "")[:8000]
                prompt = _FEEDBACK_PROMPT.format(
                    body=body, diff=diff_for_log[:12000]
                )
                loop = asyncio.get_event_loop()
                raw = await loop.run_in_executor(
                    None, _call_feedback_llm, prompt
                )
                advisory = _parse_verdict(raw)
                reason = f"{reason} | llm_advisory={advisory.get('verdict')}: {advisory.get('reason','')[:120]}"
            except Exception:
                pass

        fixlist_str = "\n".join(f"- {x}" for x in fixlist[:5]) if fixlist else ""

        tickets_mod.add_event(
            ticket.id, "feedback", "comment",
            body=f"verdict={verdict}\nreason={reason}\n{fixlist_str}",
            metadata={"feedback_verdict": verdict,
                      "feedback_reason": reason,
                      "via": "adk_workflow"},
        )

        fail_count = int(state.get(S_FAIL_COUNT, 0) or 0)
        if verdict == "fail":
            fail_count += 1

        state_delta: dict[str, Any] = {
            S_VERDICT: verdict,
            S_FIXLIST: fixlist_str or None,
            S_FAIL_COUNT: fail_count,
        }

        # The LoopAgent breaks on escalate=True. We escalate when:
        #  - verdict is pass            → fall through to learner
        #  - verdict is scope_violation → terminal, skip learner
        #  - feedback_fail_count >= cap → terminal, skip learner
        should_escalate = (
            verdict == "pass"
            or verdict == "scope_violation"
            or fail_count >= MAX_FEEDBACK_FAILS
        )

        emit(log, "adk.feedback.verdict",
             ticket=ticket.identifier,
             verdict=verdict, fail_count=fail_count,
             escalating=should_escalate)
        _emit_stage_event(
            ticket.id, "feedback", t_stage_start,
            extra={
                "verdict": verdict,
                "fail_count": fail_count,
                "compile_green": compile_green,
                "edit_block_ok": edit_block_ok,
                "test_green": test_green,
                "test_required": test_green_required,
            },
        )

        actions = EventActions(state_delta=state_delta)
        if should_escalate:
            actions = EventActions(escalate=True, state_delta=state_delta)

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text=f"verdict={verdict}\nreason={reason}\n{fixlist_str}",
                )],
            ),
            actions=actions,
        )


# ─────────────────────────── Publish agent ─────────────────────────────
class AiForgePublishAgent(BaseAgent):
    """Push the doer's commit + open a PR — gated on feedback verdict.

    Runs AFTER the doer_chain LoopAgent exits. Reads the final feedback
    verdict from session state and only publishes when verdict==pass.
    On scope_violation / fail it leaves the local commit in the
    worktree (operator can inspect, push manually, or scrap).

    Closes the loophole where ONE-7 pushed PR #107 with no controller
    and no smoke test ever running — the gate had no veto power because
    the GA runner published atomically before the gate ran.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.publish")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[publish] no ticket in state",
                ctx.invocation_id,
            )
            return
        verdict = state.get(S_VERDICT, "fail")
        worktree = state.get(S_WORKTREE)
        commit_sha = state.get("aiforge.doer.commit_sha")
        if verdict != "pass":
            emit(log, "adk.publish.skipped",
                 ticket=ticket.identifier, verdict=verdict,
                 commit_sha=commit_sha)
            tickets_mod.add_event(
                ticket.id, "publish", "comment",
                body=(f"Publish skipped — feedback verdict={verdict}. "
                      f"Local commit {commit_sha or '(none)'} left in "
                      f"worktree {worktree or '(unknown)'} for inspection."),
                metadata={"verdict": verdict, "commit_sha": commit_sha},
            )
            _emit_stage_event(
                ticket.id, "publish", t_stage_start,
                extra={"skipped": True, "verdict": verdict},
            )
            yield _yield_text_event(
                self.name, f"[publish] skipped — verdict={verdict}",
                ctx.invocation_id,
            )
            return
        if not worktree or not commit_sha:
            emit(log, "adk.publish.no_commit",
                 ticket=ticket.identifier,
                 worktree=worktree, commit_sha=commit_sha)
            yield _yield_text_event(
                self.name, "[publish] no worktree/commit to push",
                ctx.invocation_id,
            )
            return
        # Reuse the existing helper. Get the changed files list from
        # session state if available, else recompute via git.
        changed_files = state.get("aiforge.doer.changed_files") or []
        if not changed_files:
            try:
                from aiforge_core.doer.orchestrator_bridge import _run as _bridge_run
                rc, out, _ = _bridge_run(
                    ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                    cwd=worktree, timeout=15,
                )
                if rc == 0:
                    changed_files = [p.strip() for p in out.splitlines() if p.strip()]
            except Exception:
                changed_files = []
        from aiforge_core.doer.orchestrator_bridge import _git_push_pr
        emit(log, "adk.publish.start",
             ticket=ticket.identifier, commit_sha=commit_sha,
             files=len(changed_files))
        loop = asyncio.get_event_loop()
        try:
            res = await loop.run_in_executor(
                None, _git_push_pr,
                ticket, worktree, "", changed_files, log,
            )
        except Exception as exc:  # noqa: BLE001
            emit(log, "adk.publish.exception",
                 ticket=ticket.identifier, error=str(exc)[:300])
            yield _yield_text_event(
                self.name, f"[publish] exception: {exc}",
                ctx.invocation_id,
            )
            return
        emit(log, "adk.publish.done",
             ticket=ticket.identifier,
             pushed=res.get("pushed"),
             pr_url=res.get("pr_url"))
        _emit_stage_event(
            ticket.id, "publish", t_stage_start,
            extra={
                "pushed": res.get("pushed"),
                "pr_url": res.get("pr_url"),
                "commit_sha": commit_sha,
            },
        )
        comment = (
            f"Published commit {commit_sha} → "
            f"pushed={res.get('pushed')} pr={res.get('pr_url') or '(none)'}"
        )
        tickets_mod.add_event(
            ticket.id, "publish", "comment",
            body=comment,
            metadata={
                "verdict": verdict,
                "commit_sha": commit_sha,
                "pushed": res.get("pushed"),
                "pr_url": res.get("pr_url"),
            },
        )
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=comment)],
            ),
            actions=EventActions(state_delta={
                "aiforge.publish.pushed": bool(res.get("pushed")),
                "aiforge.publish.pr_url": res.get("pr_url"),
            }),
        )


# ─────────────────────────── Learner agent ──────────────────────────────
_LEARNER_PROMPT = """You are the Learner. Extract one durable :Fact from the work done on this ticket.

Rules:
- Ground every claim in the actual diff below. If the diff has no controller
  changes, do NOT claim an endpoint was added. If the diff is only a repo
  method, say "added repository method X" — nothing more.
- Use past tense. State what was actually committed, not what was planned.
- If the diff is empty, set digest to "no diff committed".

## Ticket
{body}

## Actual diff (committed to branch)
{diff}

## Most recent events
{events}

Respond with a JSON object ONLY. No prose before or after.
Keys:
- digest: one short line (<= 200 chars) grounded in the diff above.
- keywords: array of up to 5 short keywords.

Your JSON:
"""


def _call_learner_llm(prompt: str) -> str:
    from aiforge_core.llm import complete as _complete
    return _complete(
        "learner",
        [{"role": "user", "content": prompt}],
        max_tokens=16384,
        temperature=0.0,
    )


def _parse_learner(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]) if text.count("```") >= 2 else text.strip("`")
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {"digest": text[:200], "keywords": []}


def _recent_events_text(ticket_id: int, limit: int = 6) -> str:
    events = tickets_mod.comments(ticket_id, limit=limit)
    if not events:
        return "(no prior events)"
    lines = []
    for e in events:
        kind = e.get("kind") or "?"
        role = e.get("agent_role") or "?"
        body = (e.get("body") or "").replace("\n", " ")[:300]
        lines.append(f"[{role}] ({kind}) {body}")
    return "\n".join(lines)


def _write_fact_to_neo4j(
    ticket: tickets_mod.Ticket, digest: str, keywords: list[str], log
) -> None:
    """Write a :Fact node anchored to the ticket. Per agents.yaml the
    Learner is the ONLY role allowed to write :Fact, and only on
    verdict=pass. Fail-soft so a Neo4j outage cannot drop the ticket.
    """
    try:
        from aiforge_core.legacy.rag.neo4j_memory import _get_driver
        cy = (
            "MERGE (f:Fact {id: $fact_id}) "
            "ON CREATE SET f.text = $text, f.created_at = timestamp(), "
            "              f.ticket = $ticket, f.keywords = $keywords "
            "WITH f "
            "MERGE (t:Ticket {identifier: $ticket}) "
            "MERGE (f)-[:ABOUT]->(t)"
        )
        params = {
            "fact_id": f"{ticket.identifier}:fact",
            "text": digest,
            "ticket": ticket.identifier,
            "keywords": list(keywords or [])[:5],
        }
        with _get_driver().session() as s:
            s.run(cy, **params)
        emit(log, "adk.learner.fact_written",
             ticket=ticket.identifier, fact_id=params["fact_id"])
    except Exception as exc:
        emit(log, "adk.learner.fact_write_failed",
             ticket=ticket.identifier, error=str(exc)[:200])


class AiForgeLearnerAgent(BaseAgent):
    """Single-shot Learner — runs only when feedback verdict is pass.

    Per agents.yaml the Learner has no model tools; the ``write_fact``
    operation is a server-side action invoked here directly against
    Neo4j after the model returns a digest JSON.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        t_stage_start = time.time()
        state = ctx.session.state
        ticket = _ticket_from_state(state)
        log = get_logger("adk.learner")
        if ticket is None:
            yield _yield_text_event(
                self.name, "[learner] no ticket in state",
                ctx.invocation_id,
            )
            return

        verdict = state.get(S_VERDICT)
        if verdict != "pass":
            emit(log, "adk.learner.skipped",
                 ticket=ticket.identifier, verdict=verdict)
            _emit_stage_event(
                ticket.id, "learner", t_stage_start,
                extra={"skipped": True, "verdict": verdict},
            )
            yield _yield_text_event(
                self.name,
                f"[learner] skipped (verdict={verdict!r})",
                ctx.invocation_id,
            )
            return

        body = (ticket.body or "")[:4000]
        events_text = _recent_events_text(ticket.id)[:4000]
        worktree = state.get(S_WORKTREE)
        diff_text = ""
        if worktree:
            try:
                diff_text = _git_diff(worktree)[:6000]
            except Exception:
                diff_text = ""
        if not diff_text:
            diff_text = "(no diff available)"
        prompt = _LEARNER_PROMPT.format(
            body=body, diff=diff_text, events=events_text,
        )

        loop = asyncio.get_event_loop()
        try:
            raw = await loop.run_in_executor(None, _call_learner_llm, prompt)
            parsed = _parse_learner(raw)
        except Exception as exc:
            emit(log, "adk.learner.llm_error",
                 ticket=ticket.identifier, error=str(exc)[:200])
            parsed = {"digest": f"learner llm error: {exc}", "keywords": []}

        digest = (parsed.get("digest") or "")[:200] or "(empty)"
        keywords = parsed.get("keywords") or []

        _write_fact_to_neo4j(ticket, digest, keywords, log)

        tickets_mod.add_event(
            ticket.id, "learner", "comment",
            body=f"DIGEST: {digest}\nkeywords: {', '.join(keywords[:5])}",
            metadata={"source": "adk_learner_single_shot",
                      "via": "adk_workflow"},
        )

        emit(log, "adk.learner.done",
             ticket=ticket.identifier, digest_chars=len(digest))
        _emit_stage_event(
            ticket.id, "learner", t_stage_start,
            extra={"digest_chars": len(digest), "verdict": verdict},
        )

        yield _yield_text_event(
            self.name,
            f"DIGEST: {digest}\nkeywords: {', '.join(keywords[:5])}",
            ctx.invocation_id,
        )


# ─────────────────────────── Workflow factory ───────────────────────────
@dataclass
class WorkflowBundle:
    """Container returned by :func:`build_aiforge_workflow`."""
    runner: Runner
    workflow: SequentialAgent
    session_service: Any
    plugins: list[BasePlugin]


def _build_session_service():
    """Use DatabaseSessionService against aiforge Postgres if asyncpg is
    available; otherwise fall back to InMemorySessionService so the
    workflow still runs (smoke testing).
    """
    dsn_async = AIFORGE_DSN.replace(
        "postgresql://", "postgresql+asyncpg://", 1,
    ) if AIFORGE_DSN.startswith("postgresql://") else AIFORGE_DSN
    try:
        return DatabaseSessionService(db_url=dsn_async)
    except Exception:
        # asyncpg/sqlalchemy not installed — fall back so the smoke
        # path is not blocked. Production NUC ships asyncpg via
        # google-adk's transitive deps.
        return InMemorySessionService()


def build_aiforge_workflow(
    *,
    enable_neo4j_mirror: bool = True,
) -> WorkflowBundle:
    """Build the full AIForge ADK workflow.

    The structure is:

        SequentialAgent("aiforge")
          ├─ AiForgeIntentAgent("intent")            ← stage 0 (NEW)
          ├─ AiForgePlannerAgent("planner")
          ├─ LoopAgent("doer_chain", max_iterations=MAX_FEEDBACK_FAILS)
          │    ├─ AiForgeDoerAgent("doer")
          │    ├─ AiForgeIntegrationTestAgent("integration")  (gated)
          │    └─ AiForgeFeedbackAgent("feedback")
          ├─ AiForgePublishAgent("publish")
          └─ AiForgeLearnerAgent("learner")

    Intent runs first — translates plain-language ticket body into a
    structured EnrichedTicket and persists it to ticket.metadata.
    Downstream stages READ that metadata; they no longer re-classify.
    Idempotent: cached enrichment is reused.

    The LoopAgent breaks early when any sub-agent yields an Event whose
    ``actions.escalate`` is True. Feedback escalates on
    pass / scope_violation / fail_count >= cap. Doer escalates only on
    compile_fail_count >= 2 or scope_violation.
    """
    from aiforge_core.intent.agent import AiForgeIntentAgent
    intent = AiForgeIntentAgent(name="intent")
    planner = AiForgePlannerAgent(name="planner")
    doer = AiForgeDoerAgent(name="doer")
    feedback = AiForgeFeedbackAgent(name="feedback")
    publish = AiForgePublishAgent(name="publish")
    # Always include integration agent — it self-skips unless the
    # ticket auto-flags as endpoint work or AIFORGE_TEST_INTEGRATION=1.
    # AIFORGE_TEST_INTEGRATION_DISABLE=1 forces it off (kill switch
    # for the rare ticket where smoke is genuinely impossible).
    sub_agents: list[BaseAgent] = [doer]
    if os.environ.get("AIFORGE_TEST_INTEGRATION_DISABLE", "0") != "1":
        sub_agents.append(AiForgeIntegrationTestAgent(name="integration"))
    sub_agents.append(feedback)
    doer_loop = LoopAgent(
        name="doer_chain",
        sub_agents=sub_agents,
        max_iterations=MAX_FEEDBACK_FAILS,
    )
    learner = AiForgeLearnerAgent(name="learner")
    workflow = SequentialAgent(
        name="aiforge",
        sub_agents=[intent, planner, doer_loop, publish, learner],
    )

    plugins: list[BasePlugin] = []
    if enable_neo4j_mirror:
        plugins.append(Neo4jMirrorPlugin())

    session_service = _build_session_service()
    runner = Runner(
        app_name="aiforge",
        agent=workflow,
        session_service=session_service,
        plugins=plugins,
        auto_create_session=True,
    )
    return WorkflowBundle(
        runner=runner,
        workflow=workflow,
        session_service=session_service,
        plugins=plugins,
    )


__all__ = [
    "AiForgePlannerAgent",
    "AiForgeDoerAgent",
    "AiForgeFeedbackAgent",
    "AiForgeLearnerAgent",
    "Neo4jMirrorPlugin",
    "WorkflowBundle",
    "build_aiforge_workflow",
    "MAX_FEEDBACK_FAILS",
    "S_TICKET_ID",
    "S_WORKTREE",
    "S_VERDICT",
    "S_FIXLIST",
    "S_FAIL_COUNT",
    "S_COMPILE_FAIL_COUNT",
    "S_PLAN_DONE",
    "S_LAST_DOER_SUMMARY",
]
