"""aiforge_agents v0.4 — 9-stage CLI orchestrator (Understander → Learner).

This module is the **CLI / batch** entry point for the experimental
9-stage cascade. It is *not* the production runtime — that lives in
:mod:`aiforge_core.runtime.adk_runner` and is the path wired into the
HTTP API + systemd ``aiforge-graph-runner.service``.

When to use which:

================================  ==============================  =====================================
Path                              When                            Surface
================================  ==============================  =====================================
``runtime.adk_runner``            HTTP API ticket flow            5-agent ADK SequentialAgent
``aiforge_agents.orchestrator``   ``aiforge-agents-run`` CLI      9-stage cascade w/ Grounder + Validator
================================  ==============================  =====================================

Both share infrastructure that was wired up in the recovery refactor:
:mod:`aiforge_core.aiforge_agents.runtime.recovery_engine` (failure
taxonomy → Action), :mod:`aiforge_core.llm` (escalation + health +
fallback), and :func:`aiforge_core.runtime.logging_setup.get_run_logger`
(per-ticket NDJSON trace).

CLI:
    python -m aiforge_core.aiforge_agents.orchestrator.run_ticket \\
        --repo PosClientBackend \\
        --title "Add pagination to /sales endpoint" \\
        --body  "Add page+size query params; default size=50; ..."

Returns JSON to stdout with: ticket, understanding, plan, recovery, latency_s.
Per-ticket structured trace is at ``~/.aiforge/runs/<ticket_id>.ndjson``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from typing import Any

# Trigger archetype @register side effects
import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents import registry
from aiforge_core.aiforge_agents.runtime import circuit_breakers as cb_mod
from aiforge_core.aiforge_agents.learner import online as learner
from aiforge_core.runtime.logging_setup import emit, get_run_logger


def _insert_ticket_row(ticket_id: str, *, title: str, body: str,
                       repo: str, status: str,
                       log=None) -> bool:
    """Mirror to existing tickets table so the UI sees us.

    Returns True on persisted insert/update, False otherwise (psycopg
    missing, no DSN, or DB error). Errors are logged to the per-ticket
    NDJSON instead of being swallowed silently.
    """
    import os
    try:
        import psycopg
    except ImportError:
        emit(log, "ticket_db.skip", reason="psycopg_missing")
        return False
    dsn = os.environ.get("AIFORGE_DSN")
    if not dsn:
        emit(log, "ticket_db.skip", reason="no_dsn")
        return False
    try:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO tickets "
                "(identifier, title, body, status, priority, "
                " assignee_role, project, labels, metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (identifier) DO UPDATE SET "
                "  status=EXCLUDED.status, updated_at=now()",
                (ticket_id, title, body, status, "normal",
                 "aiforge_agents", repo, ["aiforge_agents"],
                 '{"runtime":"aiforge_agents","stages":4}'),
            )
        emit(log, "ticket_db.insert", status=status)
        return True
    except (psycopg.Error, OSError) as exc:
        emit(log, "ticket_db.insert_failed",
             error=str(exc)[:300], type=type(exc).__name__)
        return False


def _update_ticket_status(ticket_id: str, status: str,
                          metadata: dict | None = None,
                          log=None) -> bool:
    """Update ticket status row. Returns True on persisted update.

    Failures are logged with structured context — no silent pass.
    """
    import json as _json
    import os
    try:
        import psycopg
    except ImportError:
        emit(log, "ticket_db.skip", reason="psycopg_missing")
        return False
    dsn = os.environ.get("AIFORGE_DSN")
    if not dsn:
        emit(log, "ticket_db.skip", reason="no_dsn")
        return False
    try:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            if metadata is not None:
                cur.execute(
                    "UPDATE tickets SET status=%s, metadata=%s, "
                    "  updated_at=now() WHERE identifier=%s",
                    (status, _json.dumps(metadata), ticket_id),
                )
            else:
                cur.execute(
                    "UPDATE tickets SET status=%s, updated_at=now() "
                    "WHERE identifier=%s",
                    (status, ticket_id),
                )
        emit(log, "ticket_db.update", status=status,
             has_metadata=metadata is not None)
        return True
    except (psycopg.Error, OSError) as exc:
        emit(log, "ticket_db.update_failed",
             error=str(exc)[:300], type=type(exc).__name__,
             status=status)
        return False


def run(*, repo: str, title: str, body: str,
        ticket_id: str | None = None,
        apply: bool | None = None,
        open_mr: bool | None = None,
        route: str = "code",
        route_workflow: str | None = None) -> dict[str, Any]:
    import os as _os
    ticket_id = ticket_id or f"TKT-{uuid.uuid4().hex[:8].upper()}"
    log = get_run_logger(ticket_id, role="orchestrator")
    breakers = cb_mod.CircuitBreakers()
    from aiforge_core.aiforge_agents.runtime.recovery_engine import (
        RecoveryEngine, Action,
    )
    recovery = RecoveryEngine(log=log, breakers=breakers, ticket_id=ticket_id)
    t0 = time.time()
    if apply is None:
        apply = _os.environ.get("AIFORGE_AGENTS_APPLY", "0") not in ("0", "false", "")
    if open_mr is None:
        open_mr = _os.environ.get("AIFORGE_AGENTS_OPEN_MR", "0") not in ("0", "false", "")

    emit(log, "run.start", repo=repo, title=title[:120],
         apply=apply, open_mr=open_mr)
    learner.migrate()
    _insert_ticket_row(ticket_id, title=title, body=body,
                       repo=repo, status="processing", log=log)
    learner.record_audit(
        ticket_id=ticket_id, agent_role="orchestrator",
        event_type="ticket_started",
        payload={"repo": repo, "title": title},
    )

    # Stage U — Understander
    breakers.begin_agent("understander")
    u_agent = registry.build("understander", repo_path=None)
    u_agent.repo = repo
    u_agent.ticket_id = ticket_id
    u_t0 = time.time()
    understanding = u_agent.run(ctx={"title": title, "body": body, "repo": repo})
    u_dur = time.time() - u_t0
    breakers.check_agent("understander")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="understander",
        event_type="agent_completed",
        payload={"keys": list(understanding.keys())},
        duration_ms=int(u_dur * 1000),
    )

    # Workflow short-circuit: when route='workflow', dispatch the named
    # handler from WorkflowRegistry instead of the generic LLM cascade.
    # When route='code' (default) AND no workflow id pinned, the legacy
    # keyword-sniff path is preserved for backwards compatibility with
    # tickets created before route columns existed.
    tb_outcome = _maybe_run_workflow(
        ticket_id=ticket_id, repo=repo, title=title, body=body,
        route=route, route_workflow=route_workflow, log=log,
    )

    # Pull allowed file paths from AiForgeMemory once — Planner will
    # be constrained to pick from these.
    allowed_files = _fetch_allowed_files(
        repo=repo, query=f"{title}\n{body}", k=80, log=log,
    )

    # Pull top skills + chronic failure patterns the Learner has
    # accumulated for this repo + task_class. Both are surfaced to
    # the Planner / Doer prompts so the system auto-corrects on
    # repeat mistakes.
    task_class_guess = _guess_task_class(title, body)
    skills_hint = learner.top_skills_for(
        repo=repo, task_class=task_class_guess, k=3,
    )
    failures_hint = learner.top_failures_for(
        repo=repo, task_class=task_class_guess, k=5,
    )

    # Stage P — Planner (with REPLAN loop on grounder failure OR Verifier reject)
    p_t0 = time.time()
    plan: dict[str, Any] = {}
    grounding: dict[str, Any] = {}
    verdict: dict[str, Any] = {}
    plan_attempts = 0
    g_dur = 0.0
    v_dur = 0.0
    verifier_issues_carry: list[dict] = []
    while plan_attempts < 3:
        plan_attempts += 1
        breakers.begin_agent("planner")
        p_agent = registry.build("planner", repo_path=None)
        p_agent.repo = repo; p_agent.ticket_id = ticket_id
        # Carry verifier issues from prior attempt as additional unresolved
        # entries — Planner's prompt already understands the format.
        unresolved_for_planner = list(grounding.get("unresolved_refs", []))
        for iss in verifier_issues_carry:
            unresolved_for_planner.append({
                "step_id": iss.get("step_id", 0),
                "target": f"(verifier:{iss.get('kind','issue')})",
                "action": "verify",
                "reason": iss.get("message", "verifier_reject"),
            })
        plan = p_agent.run(ctx={
            "understanding": understanding,
            "title": title, "body": body, "repo": repo,
            "allowed_files": allowed_files,
            "skills_hint": skills_hint,
            "failures_hint": failures_hint,
            "previous_plan": plan if plan_attempts > 1 else None,
            "unresolved_refs": unresolved_for_planner,
        })
        # Post-Planner allowlist filter — drop read/edit/test/run steps
        # whose target is not in allowed_files. Planner sometimes
        # invents convention-style names ("BusinessProductsServiceImpl")
        # that don't actually exist in the repo.
        plan, dropped_refs = _filter_plan_targets(plan, allowed_files)
        breakers.check_agent("planner")

        # Grounder check
        breakers.begin_agent("grounder")
        g_agent = registry.build("grounder", repo_path=None)
        g_agent.repo = repo; g_agent.ticket_id = ticket_id
        g_t0_inner = time.time()
        grounding = g_agent.run(ctx={"plan": plan, "repo": repo})
        g_dur += time.time() - g_t0_inner
        breakers.check_agent("grounder")

        # Force REPLAN if (a) Planner returned no steps (LLM failure),
        # or (b) the post-Planner filter shrank the plan substantially,
        # or (c) Grounder rejected anything. Dropped reads = blind
        # spots for Doer.
        kept_steps = len(plan.get("steps") or [])
        total_steps = kept_steps + len(dropped_refs)
        shrink_ratio = (
            len(dropped_refs) / total_steps if total_steps else 0.0
        )
        if kept_steps == 0:
            # Surface as a synthetic unresolved so the Planner replans
            # with a clear "you returned empty" hint.
            grounding["resolved"] = False
            grounding["unresolved_refs"] = [{
                "step_id": 0, "target": "(empty plan)",
                "action": "plan", "reason": "planner_returned_no_steps",
            }]
            continue

        # Recovery engine — F-006 plan depth + repeat-replan escalation
        depth_decision = recovery.plan_depth_check(
            plan, stage="planner", attempt=plan_attempts,
        )
        if depth_decision is not None:
            if depth_decision.action is Action.SPLIT_TICKET:
                # Surface as unresolved so the planner re-attempts with
                # a smaller scope on the next loop iteration. The
                # engine has already logged the rationale.
                grounding["resolved"] = False
                grounding["unresolved_refs"] = (
                    grounding.get("unresolved_refs") or []
                ) + [{
                    "target": "(plan too deep)",
                    "action": "split",
                    "reason": depth_decision.rationale,
                    "mode_id": depth_decision.mode_id,
                }]
                if depth_decision.halt:
                    break  # ESCALATE_HUMAN — breaker tripped
                continue

        if grounding.get("resolved") and shrink_ratio < 0.5:
            # Plan is grounded — now ask Verifier for a critic pass.
            # verdict=reject re-enters the loop with verifier issues
            # as REPLAN hints. verdict in {pass, repair} is acceptable
            # (repair is for orchestrator, not a re-plan signal).
            breakers.begin_agent("verifier")
            v_agent = registry.build("verifier", repo_path=None)
            v_agent.repo = repo; v_agent.ticket_id = ticket_id
            v_t0_inner = time.time()
            verdict = v_agent.run(ctx={
                "understanding": understanding, "plan": plan,
            })
            v_dur += time.time() - v_t0_inner
            breakers.check_agent("verifier")
            if str(verdict.get("verdict", "pass")).lower() == "reject":
                verifier_issues_carry = list(
                    verdict.get("issues") or [])[:3]
                # Don't break — fall through to next plan_attempt.
                continue
            verifier_issues_carry = []
            break
        # Surface dropped refs back to Planner as if they were unresolved.
        if dropped_refs and not grounding.get("unresolved_refs"):
            grounding["unresolved_refs"] = dropped_refs
            grounding["resolved"] = False
    p_dur = time.time() - p_t0 - g_dur - v_dur  # pure planner time
    learner.record_audit(
        ticket_id=ticket_id, agent_role="planner",
        event_type="agent_completed",
        payload={"steps": len(plan.get("steps") or []),
                 "attempts": plan_attempts},
        duration_ms=int(p_dur * 1000),
    )
    learner.record_audit(
        ticket_id=ticket_id, agent_role="grounder",
        event_type="agent_completed",
        payload={"resolved": grounding.get("resolved"),
                 "unresolved_count": len(grounding.get("unresolved_refs") or []),
                 "plan_attempts": plan_attempts},
        duration_ms=int(g_dur * 1000),
    )

    # Stage V — Verifier ran inside the REPLAN loop above. If the loop
    # terminated without running it (e.g. empty plan), synthesise a
    # neutral verdict so downstream code has a stable shape.
    if not verdict:
        verdict = {"artifact_type": "verifier_verdict",
                   "verdict": "pass", "issues": [],
                   "revised_plan": None,
                   "skipped_reason": "no_grounded_plan"}
    learner.record_audit(
        ticket_id=ticket_id, agent_role="verifier",
        event_type="agent_completed",
        payload={"verdict": verdict.get("verdict"),
                 "issue_count": len(verdict.get("issues") or []),
                 "plan_attempts": plan_attempts,
                 "rejected_replans": len([
                     1 for _ in range(plan_attempts - 1)
                 ]) if str(verdict.get("verdict", "")).lower() != "reject"
                 else plan_attempts - 1},
        duration_ms=int(v_dur * 1000),
    )

    # Stage D — Multi-step Doer + Validator CRITIC loop.
    #
    # For each create/edit step in the plan, run a dedicated Doer call.
    # The local model is small; one-LLM-call-per-file caps token risk
    # and stops a single truncation from killing the whole patch set.
    # All udiffs commit onto the same `aiforge/<ticket>` branch via
    # the apply path (each Doer call appends one commit).
    #
    # Per-step CRITIC: if Validator blocks a single file, retry that
    # file once with feedback before moving on.
    doer_outcome: dict[str, Any] = {"skipped": True, "reason": "not_grounded"}
    validation: dict[str, Any] = {"decision": "skip", "reason": "no_doer"}
    d_dur = 0.0
    val_dur = 0.0
    doer_attempts = 0
    per_step_outcomes: list[dict[str, Any]] = []
    if tb_outcome is not None:
        doer_outcome = tb_outcome
        validation = {
            "artifact_type": "validation",
            "decision": ("block" if tb_outcome.get("buckets", {}).get("large", 0) > 0
                         else "approve"),
            "reason": "trial_balance_process",
        }
    elif grounding.get("resolved"):
        write_steps = [
            s for s in (plan.get("steps") or [])
            if s.get("action") in ("edit", "create")
        ]
        if not write_steps:
            doer_outcome = {"skipped": True, "reason": "no_write_step"}
        repo_path_for_rollback = _resolve_repo_path(repo) if apply else ""
        # CRITIC retry cap (bumps the per-step retry budget). Default 3:
        # Doer's first attempt + 2 retries. A diminishing-returns guard
        # below bails earlier if a retry produces no real progress.
        critic_max = max(1, int(_os.environ.get("AIFORGE_CRITIC_MAX", "3")))
        for st in write_steps:
            previous_udiff = ""
            previous_problems: list = []
            # Capture branch HEAD before this step so we can rollback the
            # commit if CRITIC retries exhaust and Validator still blocks.
            head_before_step = _git_head_sha(
                repo_path_for_rollback, branch=f"aiforge/{ticket_id}",
            ) if repo_path_for_rollback else ""
            step_committed_sha = ""
            critic_bailed = False  # diminishing-returns flag
            for attempt in range(critic_max):  # CRITIC retry per step
                doer_attempts += 1
                breakers.begin_agent("doer")
                d_agent = registry.build("doer", repo_path=None)
                d_agent.repo = repo; d_agent.ticket_id = ticket_id
                d_t0 = time.time()
                step_outcome = d_agent.run(ctx={
                    "plan": plan, "repo": repo,
                    "repo_path": _resolve_repo_path(repo),
                    "ticket_id": ticket_id,
                    "apply": apply,
                    "target_step": st,
                    "previous_udiff": previous_udiff,
                    "detector_problems": previous_problems,
                    "failures_hint": failures_hint,
                })
                d_dur += time.time() - d_t0
                breakers.check_agent("doer")
                # Recovery loop check: same Doer udiff 3x in a row for
                # the same step → F-004, escalate via engine. Halts on
                # ESCALATE_HUMAN.
                step_key = f"doer:{st.get('id') or st.get('target') or 'step'}"
                loop_decision = recovery.loop_check(
                    key=step_key,
                    output=step_outcome.get("udiff", "") or "(no_udiff)",
                    stage="doer", attempt=attempt + 1,
                )
                if loop_decision is not None and loop_decision.halt:
                    emit(log, "doer.loop_escalated",
                         step_key=step_key, mode_id=loop_decision.mode_id)
                    break
                learner.record_audit(
                    ticket_id=ticket_id, agent_role="doer",
                    event_type="agent_completed",
                    payload={
                        "step_id": st.get("id"),
                        "step_target": st.get("target"),
                        "problems": len(step_outcome.get("problems") or []),
                        "applied": step_outcome.get("applied"),
                        "attempt": attempt + 1,
                    },
                    duration_ms=int(d_dur * 1000),
                )

                # Per-step Validator
                breakers.begin_agent("validator")
                val_agent = registry.build("validator", repo_path=None)
                val_agent.repo = repo; val_agent.ticket_id = ticket_id
                val_t0 = time.time()
                step_validation = val_agent.run(
                    ctx={"doer_outcome": step_outcome},
                )
                val_dur += time.time() - val_t0
                breakers.check_agent("validator")

                if step_validation.get("decision") == "approve":
                    if step_outcome.get("applied"):
                        step_committed_sha = _git_head_sha(
                            repo_path_for_rollback,
                            branch=f"aiforge/{ticket_id}",
                        )
                    per_step_outcomes.append({
                        "step": st, "outcome": step_outcome,
                        "validation": step_validation,
                    })
                    break
                if step_validation.get("decision") == "skip":
                    per_step_outcomes.append({
                        "step": st, "outcome": step_outcome,
                        "validation": step_validation,
                    })
                    break
                # Validator blocked → consider another retry.
                cur_udiff = step_outcome.get("udiff") or ""
                cur_problems = list(step_outcome.get("problems") or [])
                # Diminishing-returns guard: if this attempt's udiff is
                # byte-identical to the prior one, OR problem count grew
                # despite feedback, retrying won't help. Bail now and
                # keep the (still-blocked) outcome for aggregation.
                no_progress = (
                    attempt > 0 and (
                        cur_udiff.strip() == previous_udiff.strip()
                        or len(cur_problems) > len(previous_problems)
                    )
                )
                if no_progress:
                    emit(log, "doer.critic_bail",
                         step_id=st.get("id"),
                         step_target=st.get("target"),
                         attempt=attempt + 1,
                         reason="diminishing_returns",
                         prev_problems=len(previous_problems),
                         cur_problems=len(cur_problems))
                    learner.record_audit(
                        ticket_id=ticket_id, agent_role="doer",
                        event_type="critic_bailed",
                        payload={
                            "step_id": st.get("id"),
                            "step_target": st.get("target"),
                            "attempt": attempt + 1,
                            "reason": "diminishing_returns",
                        },
                    )
                    critic_bailed = True
                    step_outcome = _maybe_rollback_step(
                        step_outcome=step_outcome,
                        head_before_step=head_before_step,
                        repo_path=repo_path_for_rollback,
                        ticket_id=ticket_id,
                        step=st, log=log, learner_mod=learner,
                    )
                    per_step_outcomes.append({
                        "step": st, "outcome": step_outcome,
                        "validation": step_validation,
                    })
                    break
                previous_udiff = cur_udiff
                previous_problems = cur_problems
            else:
                # All retries exhausted — rollback last attempt's commit
                # so subsequent steps + the final PR don't carry it.
                step_outcome = _maybe_rollback_step(
                    step_outcome=step_outcome,
                    head_before_step=head_before_step,
                    repo_path=repo_path_for_rollback,
                    ticket_id=ticket_id,
                    step=st, log=log, learner_mod=learner,
                )
                per_step_outcomes.append({
                    "step": st, "outcome": step_outcome,
                    "validation": step_validation,
                })

        # Aggregate the per-step results into a single doer_outcome /
        # validation pair so downstream stages and metadata stay flat.
        if per_step_outcomes:
            doer_outcome = _aggregate_doer_outcomes(
                per_step_outcomes, ticket_id=ticket_id, log=log,
            )
            validation = _aggregate_validation(per_step_outcomes)

    # Stage T — Tester (TDD test specs)
    breakers.begin_agent("tester")
    t_agent = registry.build("tester", repo_path=None)
    t_agent.repo = repo; t_agent.ticket_id = ticket_id
    t_t0 = time.time()
    test_plan = t_agent.run(ctx={
        "understanding": understanding, "plan": plan,
        "failures_hint": failures_hint,
    })
    t_dur = time.time() - t_t0
    breakers.check_agent("tester")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="tester",
        event_type="agent_completed",
        payload={"tests": len(test_plan.get("tests") or [])},
        duration_ms=int(t_dur * 1000),
    )

    # Persist Tester specs to disk + register as ticket attachment so
    # the Tester-runs-tests stage (and later forensic review) can pick
    # them up. Best-effort: any IO/DB error is logged and swallowed.
    _persist_tester_specs(
        ticket_id=ticket_id, test_plan=test_plan, log=log,
    )

    # Optional: actually invoke the repo's test runner. Opt-in via
    # AIFORGE_RUN_TESTS=1 because mvn/gradle test on a real repo can
    # take 30+ min and is irrelevant for plan-only / read-only routes.
    test_run: dict[str, Any] = {}
    if (apply and validation.get("decision") == "approve"
            and _os.environ.get("AIFORGE_RUN_TESTS", "0")
            not in ("0", "false", "")):
        test_run = _run_repo_tests(
            repo_path=_resolve_repo_path(repo),
            ticket_id=ticket_id,
            log=log,
            timeout_s=int(_os.environ.get("AIFORGE_RUN_TESTS_TIMEOUT_S",
                                          "600")),
        )
        if test_run:
            learner.record_audit(
                ticket_id=ticket_id, agent_role="tester",
                event_type="tests_executed",
                payload={
                    "framework": test_run.get("framework"),
                    "passed": test_run.get("passed", 0),
                    "failed": test_run.get("failed", 0),
                    "ok": test_run.get("ok", False),
                    "duration_s": test_run.get("duration_s", 0),
                    "timed_out": test_run.get("timed_out", False),
                },
                duration_ms=int(test_run.get("duration_s", 0) * 1000),
            )
            test_plan["execution"] = test_run

    # Stage A — Architect review + optional Doer-Architect retry.
    breakers.begin_agent("architect")
    a_agent = registry.build("architect", repo_path=None)
    a_agent.repo = repo; a_agent.ticket_id = ticket_id
    a_t0 = time.time()
    review = a_agent.run(ctx={
        "understanding": understanding, "plan": plan,
        "doer_outcome": doer_outcome, "validation": validation,
        "failures_hint": failures_hint,
        "open_mr": open_mr,
        "repo_path": _resolve_repo_path(repo),
    })
    a_dur = time.time() - a_t0
    breakers.check_agent("architect")

    # Architect→Doer retry: if Architect requested changes AND we have a
    # working udiff, run one more Doer pass with comments as feedback.
    if (review.get("decision") == "request_changes"
            and validation.get("decision") == "approve"
            and doer_outcome.get("udiff")
            and doer_attempts < 2):
        comments = list(review.get("comments") or [])[:8]
        breakers.begin_agent("doer")
        d_agent = registry.build("doer", repo_path=None)
        d_agent.repo = repo; d_agent.ticket_id = ticket_id
        d_t0 = time.time()
        doer_outcome = d_agent.run(ctx={
            "plan": plan, "repo": repo,
            "repo_path": _resolve_repo_path(repo),
            "ticket_id": ticket_id,
            "apply": apply,
            "previous_udiff": doer_outcome.get("udiff", ""),
            "detector_problems": doer_outcome.get("problems") or [],
            "architect_comments": comments,
        })
        d_dur += time.time() - d_t0
        doer_attempts += 1
        breakers.check_agent("doer")

        breakers.begin_agent("validator")
        val_agent = registry.build("validator", repo_path=None)
        val_agent.repo = repo; val_agent.ticket_id = ticket_id
        val_t0 = time.time()
        validation = val_agent.run(ctx={"doer_outcome": doer_outcome})
        val_dur += time.time() - val_t0
        breakers.check_agent("validator")

        # Re-architect on the revised diff
        breakers.begin_agent("architect")
        a_t1 = time.time()
        review = a_agent.run(ctx={
            "understanding": understanding, "plan": plan,
            "doer_outcome": doer_outcome, "validation": validation,
            "failures_hint": failures_hint,
            "open_mr": open_mr,
            "repo_path": _resolve_repo_path(repo),
        })
        a_dur += time.time() - a_t1
        breakers.check_agent("architect")

    learner.record_audit(
        ticket_id=ticket_id, agent_role="architect",
        event_type="agent_completed",
        payload={"decision": review.get("decision"),
                 "mr_title": review.get("mr_title"),
                 "doer_attempts": doer_attempts},
        duration_ms=int(a_dur * 1000),
    )

    # Stage L — Learner (writes episodic + procedural; heuristic)
    breakers.begin_agent("learner")
    l_agent = registry.build("learner", repo_path=None)
    l_agent.repo = repo; l_agent.ticket_id = ticket_id
    l_t0 = time.time()
    learning = l_agent.run(ctx={
        "ticket_id": ticket_id, "repo": repo, "plan": plan,
        "verifier_verdict": verdict, "grounding": grounding,
        "doer_outcome": doer_outcome, "validation": validation,
        "test_plan": test_plan, "review": review,
    })
    l_dur = time.time() - l_t0
    breakers.check_agent("learner")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="learner",
        event_type="agent_completed",
        payload={"outcome": learning.get("outcome"),
                 "task_class": learning.get("task_class")},
        duration_ms=int(l_dur * 1000),
    )

    total = time.time() - t0
    learner.record_episodic(
        ticket_id=ticket_id, stage="plan", agent_role="orchestrator",
        outcome="ok" if not breakers.tripped else "tripped",
        summary=f"P1 loop: U={u_dur:.1f}s P={p_dur:.1f}s total={total:.1f}s",
        artifacts={"understanding": understanding, "plan": plan},
    )

    final_status = (
        "failed"  if breakers.tripped
        else "blocked" if not grounding.get("resolved")
        else ("approved" if validation.get("decision") == "approve" else "blocked")
    )
    # HITL escalation — when terminal failures stack up, mark the ticket
    # for human review and drop a rendered request markdown alongside
    # the trace. We escalate even when final_status is "approved" if
    # tests failed, so a human can see the broken signal.
    hitl_escalate, hitl_headline, hitl_evidence = _hitl_reason(
        breakers=breakers, verdict=verdict, grounding=grounding,
        validation=validation, review=review,
        plan_attempts=plan_attempts, doer_attempts=doer_attempts,
        test_run=test_run,
    )
    hitl_path = ""
    if hitl_escalate:
        final_status = "needs_human"
        hitl_path = _persist_hitl_request(
            ticket_id=ticket_id, headline=hitl_headline,
            evidence=hitl_evidence, plan=plan,
            review=review, validation=validation,
            test_run=test_run if test_run else None, log=log,
        )
        emit(log, "hitl.escalated",
             headline=hitl_headline,
             evidence_count=len(hitl_evidence),
             request_path=hitl_path)
        learner.record_audit(
            ticket_id=ticket_id, agent_role="orchestrator",
            event_type="hitl_escalation",
            payload={"headline": hitl_headline,
                     "evidence": hitl_evidence,
                     "request_path": hitl_path},
        )
    emit(log, "run.done", final_status=final_status,
         latency_s=round(total, 2),
         tripped=breakers.tripped,
         tripped_reason=breakers.state.reason,
         hitl=hitl_escalate)
    u_slim = {k: v for k, v in understanding.items() if k != "context_md"}
    _update_ticket_status(ticket_id, final_status, log=log, metadata={
        "runtime": "aiforge_agents",
        "stages_s": {
            "understander": round(u_dur, 2),
            "planner":      round(p_dur, 2),
            "verifier":     round(v_dur, 2),
            "grounder":     round(g_dur, 2),
            "doer":         round(d_dur, 2),
            "validator":    round(val_dur, 2),
            "tester":       round(t_dur, 2),
            "architect":    round(a_dur, 2),
            "learner":      round(l_dur, 2),
        },
        "latency_s": round(total, 2),
        "verdict": verdict.get("verdict"),
        "grounded": grounding.get("resolved"),
        "unresolved_refs": grounding.get("unresolved_refs", []),
        "allowed_files_count": len(allowed_files),
        "allowed_files": allowed_files[:40],
        "understanding": u_slim,
        "plan": plan,
        "verifier": verdict,
        "grounding": grounding,
        "doer": doer_outcome,
        "validation": validation,
        "test_plan": test_plan,
        "review": review,
        "learning": learning,
    })

    return {
        "ticket_id": ticket_id,
        "repo": repo,
        "title": title,
        "understanding": understanding,
        "plan": plan,
        "verifier_verdict": verdict,
        "grounding": grounding,
        "doer_outcome": doer_outcome,
        "validation": validation,
        "test_plan": test_plan,
        "review": review,
        "learning": learning,
        "final_status": final_status,
        "hitl": {
            "escalated": hitl_escalate,
            "headline": hitl_headline if hitl_escalate else "",
            "evidence": hitl_evidence if hitl_escalate else [],
            "request_path": hitl_path,
        } if hitl_escalate else None,
        "latency_s": round(total, 2),
        "stages": {
            "understander_s": round(u_dur, 2),
            "planner_s":      round(p_dur, 2),
            "verifier_s":     round(v_dur, 2),
            "grounder_s":     round(g_dur, 2),
            "doer_s":         round(d_dur, 2),
            "validator_s":    round(val_dur, 2),
            "tester_s":       round(t_dur, 2),
            "architect_s":    round(a_dur, 2),
            "learner_s":      round(l_dur, 2),
        },
        "circuit_breaker_tripped": breakers.tripped,
        "circuit_breaker_reason":  breakers.state.reason,
        "recovery": {
            "decisions": [
                {"action": d.action.value, "mode_id": d.mode_id,
                 "rationale": d.rationale}
                for d in recovery.history
            ],
            "counts": dict(recovery._counts),
        },
    }


def _aggregate_doer_outcomes(
    per_step: list[dict[str, Any]],
    *, ticket_id: str = "", log=None,
) -> dict[str, Any]:
    """Merge per-step Doer outcomes into one doer_outcome dict.

    Concatenates udiffs (delimited), unions problems, picks last
    branch/applied state. Useful for downstream Validator/Architect
    that still expect a single `doer_outcome`.

    Full diff is persisted to ``~/.aiforge/artifacts/<ticket>/full.diff``;
    the in-memory ``udiff`` field is capped at 64 KiB (was 8 KiB) so
    big multi-file PRs no longer lose their tail when LLMs serialise it.
    The cap is overridable via ``AIFORGE_AGGREGATE_UDIFF_BYTES``.
    """
    import os as _os
    from pathlib import Path as _Path

    udiffs: list[str] = []
    problems: list[dict[str, Any]] = []
    targets: list[str] = []
    applied_any = False
    applied_branch = ""
    apply_errors: list[str] = []
    for entry in per_step:
        out = entry.get("outcome") or {}
        if out.get("udiff"):
            udiffs.append(
                f"### step {entry['step'].get('id')} "
                f"({entry['step'].get('action')}) "
                f"-> {entry['step'].get('target')}\n"
                + out["udiff"]
            )
        problems.extend(out.get("problems") or [])
        if out.get("target"):
            targets.append(out["target"])
        if out.get("applied"):
            applied_any = True
        if out.get("applied_branch"):
            applied_branch = out["applied_branch"]
        if out.get("apply_error"):
            apply_errors.append(
                f"{entry['step'].get('target')}: {out['apply_error']}"
            )

    full_udiff = "\n\n".join(udiffs)
    try:
        cap = int(_os.environ.get("AIFORGE_AGGREGATE_UDIFF_BYTES", "65536"))
    except ValueError:
        cap = 65536
    truncated = len(full_udiff.encode("utf-8")) > cap
    artifact_path = ""
    if ticket_id and full_udiff:
        try:
            base = _Path(_os.path.expanduser(
                "~/.aiforge/artifacts")) / ticket_id
            base.mkdir(parents=True, exist_ok=True)
            target = base / "full.diff"
            target.write_text(full_udiff, encoding="utf-8")
            artifact_path = str(target)
            emit(log, "doer.full_diff_persisted",
                 path=artifact_path, bytes=len(full_udiff))
        except OSError as exc:
            emit(log, "doer.full_diff_persist_failed",
                 error=str(exc)[:200], type=type(exc).__name__)

    return {
        "artifact_type": "doer_outcome",
        "step_count": len(per_step),
        "target": targets[0] if targets else "",
        "targets": targets,
        "udiff": full_udiff[:cap],
        "udiff_truncated": truncated,
        "udiff_bytes": len(full_udiff.encode("utf-8")),
        "udiff_artifact_path": artifact_path,
        "problems": problems,
        "applied": applied_any,
        "applied_branch": applied_branch,
        "apply_error": "; ".join(apply_errors) if apply_errors else "",
        "blocked_by_detectors": len(problems) > 0,
        "tests_green": False,
    }


def _aggregate_validation(
    per_step: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validator decision rollup — block if any step blocked, skip if
    all skipped, approve otherwise."""
    decisions = [
        (e.get("validation") or {}).get("decision", "skip")
        for e in per_step
    ]
    if any(d == "block" for d in decisions):
        return {"artifact_type": "validation",
                "decision": "block",
                "reason": "step_blocked",
                "step_decisions": decisions}
    if all(d == "skip" for d in decisions):
        return {"artifact_type": "validation",
                "decision": "skip",
                "reason": "all_skipped",
                "step_decisions": decisions}
    return {"artifact_type": "validation",
            "decision": "approve",
            "reason": "all_approved",
            "step_decisions": decisions}


def _filter_plan_targets(
    plan: dict[str, Any], allowed: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Drop read|edit|test|run steps whose `target` is not in `allowed`.

    Returns (filtered_plan, dropped_steps). `create` and `search`
    actions are kept verbatim (Grounder validates them separately).
    """
    if not allowed:
        return plan, []
    allowed_set = set(allowed)
    allowed_basenames = {p.rsplit("/", 1)[-1] for p in allowed}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for st in (plan.get("steps") or []):
        action = (st.get("action") or "").lower()
        if action in ("create", "search"):
            kept.append(st)
            continue
        tgt = (st.get("target") or "").strip()
        if not tgt:
            kept.append(st)
            continue
        if tgt in allowed_set:
            kept.append(st)
            continue
        bn = tgt.rsplit("/", 1)[-1]
        if bn in allowed_basenames:
            for ap in allowed:
                if ap.endswith("/" + bn) or ap == bn:
                    kept.append({**st, "target": ap})
                    break
            continue
        # Hallucinated reference — Planner invented this filename.
        dropped.append({"step_id": st.get("id"), "target": tgt,
                        "action": action, "reason": "not_in_allowlist"})
    out = dict(plan)
    out["steps"] = kept
    return out, dropped


def _maybe_run_workflow(*, ticket_id: str, repo: str,
                        title: str, body: str,
                        route: str = "code",
                        route_workflow: str | None = None,
                        log=None) -> dict | None:
    """Registry-based workflow dispatch.

    Order of resolution:
      1. ``route == 'workflow'`` AND ``route_workflow`` set → dispatch
         the named handler (preferred — chosen by detector or human
         override at ticket POST time).
      2. ``route == 'code'`` AND no workflow id → run the legacy keyword
         sniff for backwards compatibility with tickets created before
         the route columns existed.
      3. Otherwise → return None (LLM cascade runs).

    Returns the doer-outcome-shaped dict from the handler, or None.
    """
    from aiforge_core.workflows import REGISTRY, dispatch
    if route == "workflow" and route_workflow:
        spec = REGISTRY.get(route_workflow)
        if spec is None:
            emit(log, "workflow.unknown_id", route_workflow=route_workflow)
            return {
                "artifact_type": "doer_outcome",
                "process": route_workflow,
                "applied": False,
                "udiff": "",
                "problems": [{
                    "mode": "workflow_unknown",
                    "evidence": f"workflow {route_workflow!r} not registered",
                }],
                "blocked_by_detectors": True,
            }
        emit(log, "workflow.dispatch", workflow=route_workflow,
             handler=spec.handler)
        try:
            ticket = {
                "id": ticket_id, "identifier": ticket_id,
                "title": title, "body": body, "repo": repo,
            }
            return dispatch(route_workflow, ticket, log=log)
        except (ImportError, OSError, ValueError, KeyError) as exc:
            emit(log, "workflow.dispatch_failed",
                 workflow=route_workflow,
                 error=str(exc)[:300], type=type(exc).__name__)
            return {
                "artifact_type": "doer_outcome",
                "process": route_workflow,
                "applied": False,
                "udiff": "",
                "problems": [{
                    "mode": "workflow_dispatch_exception",
                    "evidence": str(exc)[:500],
                    "exc_type": type(exc).__name__,
                }],
                "blocked_by_detectors": True,
            }

    # Legacy keyword sniff — only runs when the ticket predates the
    # route columns (route='code' default + no workflow id). Once the
    # detector backfills route on every POST, this path stops firing.
    return _maybe_run_trial_balance(
        ticket_id=ticket_id, repo=repo, title=title, body=body,
    )


def _maybe_run_trial_balance(*, ticket_id: str, repo: str,
                             title: str, body: str) -> dict | None:
    """If the ticket has tally + oneshell attachments AND `trial-balance`
    in title/body/labels, run the deterministic reconciliation and
    return a doer-outcome-shaped dict. Else None.
    """
    text = (title + " " + body).lower()
    if "trial" not in text and "trial-balance" not in text:
        return None
    atts = learner.attachments_for(ticket_id)
    by_role: dict[str, dict] = {}
    for a in atts:
        by_role.setdefault(a["role"], a)
    if "tally" not in by_role or "oneshell" not in by_role:
        return None
    try:
        from aiforge_core.aiforge_agents.processes import trial_balance as tb
        from pathlib import Path
        env = "prod" if "prod" in text else "qa"
        # Stage 1: validate files first — clear schema feedback
        fv_t = tb.validate_file(by_role["tally"]["file_path"], expected="tally")
        fv_o = tb.validate_file(by_role["oneshell"]["file_path"], expected="oneshell")
        validation_errors = []
        for fv in (fv_t, fv_o):
            for e in fv.errors:
                validation_errors.append(f"{Path(fv.path).name}: {e}")
        if validation_errors:
            return {
                "artifact_type": "doer_outcome",
                "process": "trial_balance",
                "env": env,
                "applied": False,
                "udiff": "",
                "target": "trial-balance-report.md",
                "problems": [{"mode": "file_validation",
                              "evidence": e} for e in validation_errors],
                "blocked_by_detectors": True,
                "file_validation": {
                    "tally": {"ok": fv_t.ok, "errors": fv_t.errors,
                              "warnings": fv_t.warnings,
                              "rows": fv_t.row_count},
                    "oneshell": {"ok": fv_o.ok, "errors": fv_o.errors,
                                 "warnings": fv_o.warnings,
                                 "rows": fv_o.row_count},
                },
            }
        # Stage 2: parse + optional Mongo fetch
        t_rows = tb.parse_tally(Path(by_role["tally"]["file_path"]))
        o_file = tb.parse_oneshell(Path(by_role["oneshell"]["file_path"]))
        # Try to pull live OneShell rows from Mongo for a 3-way recon.
        # Best-effort: if Mongo isn't reachable, fall back to 2-way.
        bid = ""
        m = re.search(r"\bb\d{14,}\b", text)  # business id heuristic
        if m:
            bid = m.group(0)
        o_db: list = []
        if bid:
            # Authoritative path: PCB's /trialBalance HTTP endpoint
            # (computes from transactions). Falls back to direct Mongo
            # which only has opening balances when PCB API unreachable.
            import datetime as _dt
            today = _dt.date.today()
            fy_year = today.year + (1 if today.month >= 4 else 0)
            try:
                o_db = tb.fetch_oneshell_via_api(
                    business_id=bid, env=env,
                    financial_year=fy_year,
                )
            except Exception:
                try:
                    o_db = tb.fetch_oneshell_from_mongo(
                        business_id=bid, env=env,
                    )
                except Exception:
                    o_db = []
        # Top-down verify: compare totals first, drill down only on mismatch
        rep = tb.verify(
            tally=t_rows, file_rows=o_file,
            db_rows=o_db or None, api_rows=None,
            env=env, business_id=bid,
        )
        md = tb.render_verify_report(rep)
        # Build a flat-keyed view of top-line totals for the UI
        totals_view = {
            src: {"OB": t.total_ob, "DR": t.total_dr,
                  "CR": t.total_cr, "CB": t.total_cb,
                  "rows": t.row_count}
            for src, t in rep.totals.items()
        }
        if o_db:
            three = tb.reconcile_3way(
                t_rows, o_file, o_db, env=env, business_id=bid,
            )
            any_large = any([
                three.file_vs_db.large,
                three.tally_vs_file.large,
                three.tally_vs_db.large,
            ])
            return {
                "artifact_type": "doer_outcome",
                "process": "trial_balance",
                "mode": "3way+verify",
                "env": env,
                "udiff": md,
                "target": "trial-balance-report.md",
                "totals": totals_view,
                "top_line_mismatch": rep.has_top_line_mismatch,
                "drilled_accounts": len([d for d in rep.drill
                                         if d.diagnoses != ["ok — totals within tolerance"]]),
                "file_vs_db_gap": three.file_vs_db.gap,
                "tally_vs_file_gap": three.tally_vs_file.gap,
                "tally_vs_db_gap":  three.tally_vs_db.gap,
                "buckets": {
                    "tally_vs_db_match": len(three.tally_vs_db.matched),
                    "tally_vs_db_large": len(three.tally_vs_db.large),
                    "file_vs_db_large":  len(three.file_vs_db.large),
                },
                "applied": False,
                "problems": [],
                "blocked_by_detectors": (any_large or rep.has_top_line_mismatch),
            }
        # 2-way fallback when no live DB rows
        rec = tb.reconcile(t_rows, o_file, env=env, business_id=bid)
        return {
            "artifact_type": "doer_outcome",
            "process": "trial_balance",
            "mode": "2way+verify",
            "env": env,
            "udiff": md,                      # already from verify()
            "target": "trial-balance-report.md",
            "totals": totals_view,
            "top_line_mismatch": rep.has_top_line_mismatch,
            "drilled_accounts": len([d for d in rep.drill
                                     if d.diagnoses != ["ok — totals within tolerance"]]),
            "tally_total": rec.tally_total,
            "oneshell_total": rec.oneshell_total,
            "gap": rec.gap,
            "buckets": {
                "match": len(rec.matched),
                "diff":  len(rec.diff),
                "large": len(rec.large),
                "tally_only":    len(rec.tally_only),
                "oneshell_only": len(rec.oneshell_only),
            },
            "applied": False,
            "problems": [],
            "blocked_by_detectors": rep.has_top_line_mismatch,
        }
    except (ImportError, OSError, ValueError, KeyError) as exc:
        # Specific failures only — don't swallow KeyboardInterrupt or
        # SystemExit. Log structured error so the run trace shows root
        # cause, not just a stringified message.
        import logging as _logging
        _logging.getLogger("aiforge.trial_balance").exception(
            "trial_balance.process_failed",
            extra={"aiforge": {"ticket": ticket_id, "repo": repo,
                               "type": type(exc).__name__}},
        )
        return {
            "artifact_type": "doer_outcome",
            "process": "trial_balance",
            "error": f"validator_failed: {type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
            "applied": False,
            "problems": [{
                "mode": "trial_balance_exception",
                "evidence": str(exc)[:500],
                "exc_type": type(exc).__name__,
            }],
            "udiff": "",
        }


def _guess_task_class(title: str, body: str) -> str:
    """Cheap keyword-based task_class for skill lookup. Same vocabulary
    Learner derives from doer_outcome.target's feature dir name."""
    text = (title + " " + body).lower()
    cues = (
        ("readme", "readme"), ("documentation", "readme"),
        ("test", "test"), ("validation", "test"),
        ("crud", "feature"), ("endpoint", "feature"),
        ("api", "feature"), ("controller", "feature"),
        ("sync", "datasync"), ("nats", "datasync"),
        ("auth", "auth"), ("jwt", "auth"), ("login", "auth"),
        ("ledger", "ledger"), ("sales", "sales"),
    )
    for kw, cls in cues:
        if kw in text:
            return cls
    return "unknown"


def _hitl_reason(*, breakers, verdict: dict, grounding: dict,
                 validation: dict, review: dict,
                 plan_attempts: int, doer_attempts: int,
                 test_run: dict | None) -> tuple[bool, str, list[str]]:
    """Decide whether to escalate to a human + collect rationale.

    Returns ``(escalate, headline, evidence)`` where evidence is a list
    of short strings rendered into the HITL request markdown.

    Escalation triggers (any):
      - circuit breaker tripped (LLM unavailable / repeated panic)
      - Verifier still rejects after the 3-attempt REPLAN cap
      - Grounder never resolved (Planner couldn't anchor in real files)
      - Validator blocked AND CRITIC retries already used >= 3 attempts
      - Architect rejected the diff
      - Tests actually ran AND failed
    """
    evidence: list[str] = []
    headline = ""
    if getattr(breakers, "tripped", False):
        evidence.append(
            f"Circuit breaker tripped: {getattr(breakers.state, 'reason', '?')}"
        )
        headline = "circuit_breaker"
    if (str(verdict.get("verdict", "")).lower() == "reject"
            and plan_attempts >= 3):
        evidence.append(
            f"Verifier rejected the plan after {plan_attempts} REPLAN attempts."
        )
        headline = headline or "verifier_persistent_reject"
    if not grounding.get("resolved"):
        unresolved = grounding.get("unresolved_refs") or []
        evidence.append(
            f"Grounder failed to resolve {len(unresolved)} reference(s); "
            "Planner couldn't anchor in real files."
        )
        headline = headline or "grounder_unresolved"
    if validation.get("decision") == "block" and doer_attempts >= 3:
        evidence.append(
            f"Validator blocked the diff after {doer_attempts} Doer attempts: "
            f"{validation.get('reason', '?')}"
        )
        headline = headline or "validator_persistent_block"
    if str(review.get("decision", "")).lower() == "reject":
        evidence.append(
            f"Architect rejected the diff: "
            f"{(review.get('comments') or [''])[0][:200]}"
        )
        headline = headline or "architect_reject"
    if test_run and not test_run.get("ok") and test_run.get("framework"):
        evidence.append(
            f"Test run failed ({test_run['framework']}): "
            f"passed={test_run.get('passed', 0)} "
            f"failed={test_run.get('failed', 0)} "
            f"timed_out={test_run.get('timed_out', False)}"
        )
        headline = headline or "tests_failed"
    return (bool(evidence), headline or "unknown", evidence)


def _persist_hitl_request(*, ticket_id: str, headline: str,
                          evidence: list[str], plan: dict,
                          review: dict, validation: dict,
                          test_run: dict | None, log) -> str:
    """Write a human-readable HITL request alongside the run trace.

    Returns absolute path on success, "" on IO failure. Best-effort —
    never raised so HITL never makes a bad situation worse."""
    import os as _os
    from pathlib import Path

    try:
        runs_root = Path(
            _os.environ.get("AIFORGE_RUNS_DIR")
            or _os.path.expanduser("~/.aiforge/runs"),
        )
        ticket_dir = runs_root / ticket_id
        ticket_dir.mkdir(parents=True, exist_ok=True)
        req_path = ticket_dir / "hitl_request.md"
        lines = [
            f"# Human review requested — {ticket_id}",
            "",
            f"**Headline:** `{headline}`",
            "",
            "## Evidence",
            "",
        ]
        lines += [f"- {e}" for e in evidence]
        lines += [
            "",
            "## Snapshot",
            "",
            f"- plan steps: {len(plan.get('steps') or [])}",
            f"- architect decision: `{review.get('decision', '?')}`",
            f"- validator decision: `{validation.get('decision', '?')}`",
        ]
        if test_run:
            lines.append(
                f"- test run: framework=`{test_run.get('framework', '?')}` "
                f"passed={test_run.get('passed', 0)} "
                f"failed={test_run.get('failed', 0)}"
            )
        if review.get("comments"):
            lines += ["", "## Architect comments", ""]
            for c in (review.get("comments") or [])[:6]:
                lines.append(f"- {str(c)[:300]}")
        req_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        emit(log, "hitl.persist_failed", error=str(exc)[:200])
        return ""

    try:
        learner.add_attachment(
            ticket_id=ticket_id, filename="hitl_request.md",
            file_path=str(req_path), role="hitl_request",
            content_type="text/markdown",
            bytes_=req_path.stat().st_size,
        )
    except Exception:  # noqa: BLE001
        pass
    return str(req_path)


def _detect_test_framework(repo_path: str) -> tuple[str, list[str]] | None:
    """Inspect ``repo_path`` and pick a test command.

    Returns ``(framework_name, argv)`` or None if no obvious test runner
    is wired up. Order matters — Maven first because Java repos can also
    have `package.json` for tooling but their primary test runner is mvn.
    """
    from pathlib import Path
    if not repo_path:
        return None
    root = Path(repo_path)
    if not root.is_dir():
        return None
    if (root / "pom.xml").is_file():
        # `-q` quiet, `-DskipITs` skip integration tests (long runners),
        # `-Dmaven.javadoc.skip=true` cuts noise.
        return ("maven", ["./mvnw", "test", "-q",
                          "-DskipITs=true",
                          "-Dmaven.javadoc.skip=true"]) \
            if (root / "mvnw").is_file() else \
            ("maven", ["mvn", "test", "-q",
                       "-DskipITs=true",
                       "-Dmaven.javadoc.skip=true"])
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return ("gradle",
                ["./gradlew", "test", "--quiet"]
                if (root / "gradlew").is_file()
                else ["gradle", "test", "--quiet"])
    if (root / "package.json").is_file():
        # `npm test --silent` so we don't drown in lifecycle noise.
        return ("npm", ["npm", "test", "--silent"])
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        return ("pytest", ["pytest", "-q"])
    return None


def _summarise_test_run(framework: str, stdout: str, stderr: str,
                        exit_code: int) -> dict:
    """Best-effort pass/fail counts from each runner's tail output.

    Pure regex/string-parse — no XML parsing, surefire report scraping,
    or coverage merging. Good enough for the audit row + dashboard."""
    import re
    passed = failed = 0
    if framework == "pytest":
        # `=== 12 passed, 1 failed in 3.45s ===`
        m_pass = re.search(r"(\d+)\s+passed", stdout)
        m_fail = re.search(r"(\d+)\s+failed", stdout)
        if m_pass:
            passed = int(m_pass.group(1))
        if m_fail:
            failed = int(m_fail.group(1))
    elif framework in ("maven", "gradle"):
        # `Tests run: 12, Failures: 0, Errors: 0, Skipped: 0`
        m = re.search(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)",
            stdout,
        )
        if m:
            run = int(m.group(1))
            fails = int(m.group(2)) + int(m.group(3))
            failed = fails
            passed = max(0, run - fails)
    elif framework == "npm":
        # jest: "Tests:  12 passed, 1 failed, 13 total"
        m_pass = re.search(r"(\d+)\s+passed", stdout + stderr)
        m_fail = re.search(r"(\d+)\s+failed", stdout + stderr)
        if m_pass:
            passed = int(m_pass.group(1))
        if m_fail:
            failed = int(m_fail.group(1))
    return {
        "framework": framework,
        "exit_code": exit_code,
        "passed": passed,
        "failed": failed,
        "ok": exit_code == 0,
    }


def _run_repo_tests(*, repo_path: str, ticket_id: str, log,
                    timeout_s: int = 300) -> dict:
    """Detect framework + execute its test command. Returns a dict
    suitable for audit + downstream gating. Empty dict if no runner."""
    import os as _os
    import subprocess
    import time as _time

    detected = _detect_test_framework(repo_path)
    if detected is None:
        emit(log, "tester.no_framework_detected", repo_path=repo_path)
        return {}
    framework, argv = detected
    emit(log, "tester.run_started",
         framework=framework, argv=" ".join(argv))
    t0 = _time.time()
    try:
        proc = subprocess.run(
            argv, cwd=repo_path, capture_output=True, text=True,
            timeout=timeout_s,
        )
        timed_out = False
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or b"").decode("utf-8", "replace")
        stderr = (exc.stderr or b"").decode("utf-8", "replace")
        exit_code = -1
        emit(log, "tester.run_timed_out",
             framework=framework, timeout_s=timeout_s)
    except (OSError, FileNotFoundError) as exc:
        emit(log, "tester.run_spawn_failed",
             framework=framework, error=str(exc)[:200])
        return {"framework": framework, "exit_code": -2, "ok": False,
                "error": str(exc)[:200]}
    duration_s = round(_time.time() - t0, 3)
    summary = _summarise_test_run(framework, stdout, stderr, exit_code)
    summary["duration_s"] = duration_s
    summary["timed_out"] = timed_out
    summary["stdout_tail"] = stdout[-2000:]
    summary["stderr_tail"] = stderr[-2000:]
    emit(log, "tester.run_completed",
         framework=framework, ok=summary["ok"],
         passed=summary.get("passed", 0),
         failed=summary.get("failed", 0),
         duration_s=duration_s)
    return summary


def _persist_tester_specs(*, ticket_id: str, test_plan: dict, log) -> str:
    """Write Tester output to ``~/.aiforge/runs/<ticket>/test_plan.json``
    and register it as an attachment with role=tester_specs.

    Returns the absolute path on success, "" on failure. Best-effort —
    IO and DB errors are logged via the ticket NDJSON, never raised."""
    import json as _json
    import os as _os
    from pathlib import Path

    if not test_plan or not isinstance(test_plan, dict):
        return ""
    tests = list(test_plan.get("tests") or [])
    try:
        runs_root = Path(
            _os.environ.get("AIFORGE_RUNS_DIR")
            or _os.path.expanduser("~/.aiforge/runs"),
        )
        ticket_dir = runs_root / ticket_id
        ticket_dir.mkdir(parents=True, exist_ok=True)
        specs_path = ticket_dir / "test_plan.json"
        specs_path.write_text(
            _json.dumps(test_plan, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        size = specs_path.stat().st_size
    except OSError as exc:
        emit(log, "tester.specs_persist_failed", error=str(exc)[:200])
        return ""

    try:
        learner.add_attachment(
            ticket_id=ticket_id,
            filename="test_plan.json",
            file_path=str(specs_path),
            role="tester_specs",
            content_type="application/json",
            bytes_=size,
        )
    except Exception as exc:  # noqa: BLE001
        # add_attachment already swallows DB errors; this is a paranoia
        # belt-and-braces against unexpected typing issues. Disk write
        # already succeeded — keep going.
        emit(log, "tester.specs_attachment_failed", error=str(exc)[:200])

    emit(log, "tester.specs_persisted",
         path=str(specs_path), test_count=len(tests))
    learner.record_audit(
        ticket_id=ticket_id, agent_role="tester",
        event_type="specs_persisted",
        payload={"path": str(specs_path), "test_count": len(tests),
                 "bytes": size},
    )
    return str(specs_path)


def _resolve_repo_path(repo_name: str) -> str:
    """Best-effort: $AIFORGE_WORKTREE_ROOT/<repo_name> if it exists."""
    import os
    from pathlib import Path
    root = os.environ.get(
        "AIFORGE_WORKTREE_ROOT",
        os.path.expanduser("~/codeRepo"),
    )
    p = Path(root) / repo_name
    return str(p) if p.is_dir() else ""


def _git_head_sha(repo_path: str, branch: str) -> str:
    """Return current HEAD sha on ``branch`` or "" if unavailable."""
    if not repo_path:
        return ""
    import subprocess
    from pathlib import Path
    if not Path(repo_path).is_dir():
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _maybe_rollback_step(
    *, step_outcome: dict, head_before_step: str,
    repo_path: str, ticket_id: str,
    step: dict, log, learner_mod,
) -> dict:
    """Rollback the step's commit if it was applied. Returns mutated copy
    of ``step_outcome`` with rolled_back/rolled_back_to set."""
    if not (step_outcome.get("applied")
            and head_before_step
            and repo_path):
        return step_outcome
    cur = _git_head_sha(repo_path, branch=f"aiforge/{ticket_id}")
    if not cur or cur == head_before_step:
        return step_outcome
    ok = _git_reset_to(repo_path, head_before_step)
    out = dict(step_outcome)
    out["rolled_back"] = ok
    if ok:
        out["rolled_back_to"] = head_before_step
        emit(log, "doer.step_rolled_back",
             step_id=step.get("id"),
             step_target=step.get("target"),
             from_sha=cur[:8], to_sha=head_before_step[:8])
        learner_mod.record_audit(
            ticket_id=ticket_id, agent_role="doer",
            event_type="step_rolled_back",
            payload={"step_id": step.get("id"),
                     "step_target": step.get("target"),
                     "from_sha": cur, "to_sha": head_before_step},
        )
    else:
        emit(log, "doer.step_rollback_failed",
             step_id=step.get("id"),
             step_target=step.get("target"))
    return out


def _git_reset_to(repo_path: str, sha: str) -> bool:
    """Hard-reset the working tree to ``sha``. Returns True on success.

    Used to drop a step's commit when CRITIC exhausts and Validator still
    blocks. Only resets when ``sha`` is non-empty so we never accidentally
    nuke uncommitted work."""
    if not repo_path or not sha:
        return False
    import subprocess
    from pathlib import Path
    if not Path(repo_path).is_dir():
        return False
    try:
        out = subprocess.run(
            ["git", "reset", "--hard", sha],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _fetch_allowed_files(*, repo: str, query: str, k: int = 80,
                         log=None) -> list[str]:
    """Pull top-K relevant File_v2 paths from AiForgeMemory for this query.

    Vector + fulltext hybrid via translator. Used to constrain Planner
    output. Each backend (vector / fulltext / service) is best-effort —
    failures are logged with source label so a degraded run is visible.
    """
    try:
        import os
        from neo4j import GraphDatabase
        from aiforge_memory.query.translator import (
            _embed_query, _vector_topk, _fulltext_symbols,
        )
    except ImportError as exc:
        emit(log, "allowed_files.import_failed", error=str(exc)[:200])
        return []

    try:
        drv = GraphDatabase.driver(
            os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
            auth=(
                os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
                os.environ.get("AIFORGE_NEO4J_PASSWORD", "password"),
            ),
        )
    except Exception as exc:  # neo4j driver raises a subclass tree
        emit(log, "allowed_files.driver_failed",
             error=str(exc)[:200], type=type(exc).__name__)
        return []

    def _ok(p: str) -> bool:
        """Drop noise: prior agent worktree dirs and dotfiles outside src."""
        if not p:
            return False
        if ".aiforge-worktrees/" in p:
            return False
        if p.startswith(".aiforge/"):
            return False
        return True

    files: list[str] = []
    sources_used: list[str] = []
    try:
        # Vector hits
        try:
            vec = _embed_query(query)
            added = 0
            for r in _vector_topk(drv, repo=repo, vec=vec, k=k * 3):
                fp = r.get("file_path")
                if _ok(fp) and fp not in files:
                    files.append(fp); added += 1
            sources_used.append(f"vector:{added}")
        except Exception as exc:
            emit(log, "allowed_files.vector_failed",
                 error=str(exc)[:200], type=type(exc).__name__)

        # Fulltext hits
        try:
            _, ft_files = _fulltext_symbols(drv, repo=repo, text=query, k=k * 3)
            added = 0
            for fp in ft_files:
                if _ok(fp) and fp not in files:
                    files.append(fp); added += 1
            sources_used.append(f"fulltext:{added}")
        except Exception as exc:
            emit(log, "allowed_files.fulltext_failed",
                 error=str(exc)[:200], type=type(exc).__name__)

        # Top-N from each Service's CONTAINS_FILE (always include service files)
        try:
            with drv.session() as s:
                rows = list(s.run(
                    "MATCH (:Service {repo:$repo})-[:CONTAINS_FILE]->(f:File_v2) "
                    "RETURN f.path AS p LIMIT $k",
                    repo=repo, k=k * 3,
                ))
            added = 0
            for r in rows:
                p = r["p"]
                if _ok(p) and p not in files:
                    files.append(p); added += 1
            sources_used.append(f"service:{added}")
        except Exception as exc:
            emit(log, "allowed_files.service_failed",
                 error=str(exc)[:200], type=type(exc).__name__)
    finally:
        try:
            drv.close()
        except Exception as exc:
            emit(log, "allowed_files.close_failed",
                 error=str(exc)[:120])

    emit(log, "allowed_files.done", repo=repo,
         total=len(files), capped_at=k, sources=sources_used)
    return files[:k]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-agents-run")
    p.add_argument("--repo", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--ticket-id")
    p.add_argument(
        "--route", choices=["code", "workflow", "auto"], default="auto",
        help="'code' = LLM cascade, 'workflow' = named handler, "
             "'auto' = run detector against title/body/attachments",
    )
    p.add_argument(
        "--workflow",
        help="Workflow id when --route=workflow (e.g. tally-trial-balance). "
             "Ignored when --route=code/auto.",
    )
    a = p.parse_args(argv)

    route = a.route
    workflow_id = a.workflow
    if route == "auto":
        from aiforge_core.workflows import detect_route
        # Attachments aren't known at CLI invocation time — feed empty
        # list. The detector will still match keyword-only workflows
        # and let the operator override with --route + --workflow.
        decided = detect_route(title=a.title, body=a.body, attachments=[])
        route = decided.kind
        workflow_id = decided.workflow_id

    out = run(repo=a.repo, title=a.title, body=a.body,
              ticket_id=a.ticket_id,
              route=route, route_workflow=workflow_id)
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["circuit_breaker_tripped"] else 1


if __name__ == "__main__":
    sys.exit(main())
