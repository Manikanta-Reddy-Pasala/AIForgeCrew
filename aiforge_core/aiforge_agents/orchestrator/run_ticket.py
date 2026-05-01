"""Minimal P1 orchestrator entry — runs Understander → Planner on a ticket.

CLI:
    python -m aiforge_core.aiforge_agents.orchestrator.run_ticket \\
        --repo PosClientBackend \\
        --title "Add pagination to /sales endpoint" \\
        --body  "Add page+size query params; default size=50; ..."

Returns JSON to stdout with: ticket, understanding, plan, latency_s.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

# Trigger archetype @register side effects
import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents import registry
from aiforge_core.aiforge_agents.runtime import circuit_breakers as cb_mod
from aiforge_core.aiforge_agents.learner import online as learner


def _insert_ticket_row(ticket_id: str, *, title: str, body: str,
                       repo: str, status: str) -> None:
    """Mirror to existing tickets table so the UI sees us."""
    import os
    try:
        import psycopg
    except ImportError:
        return
    dsn = os.environ.get("AIFORGE_DSN")
    if not dsn:
        return
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
    except Exception:
        pass


def _update_ticket_status(ticket_id: str, status: str,
                          metadata: dict | None = None) -> None:
    import json as _json
    import os
    try:
        import psycopg
    except ImportError:
        return
    dsn = os.environ.get("AIFORGE_DSN")
    if not dsn:
        return
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
    except Exception:
        pass


def run(*, repo: str, title: str, body: str,
        ticket_id: str | None = None,
        apply: bool | None = None,
        open_mr: bool | None = None) -> dict[str, Any]:
    import os as _os
    ticket_id = ticket_id or f"TKT-{uuid.uuid4().hex[:8].upper()}"
    breakers = cb_mod.CircuitBreakers()
    t0 = time.time()
    if apply is None:
        apply = _os.environ.get("AIFORGE_AGENTS_APPLY", "0") not in ("0", "false", "")
    if open_mr is None:
        open_mr = _os.environ.get("AIFORGE_AGENTS_OPEN_MR", "0") not in ("0", "false", "")

    learner.migrate()
    _insert_ticket_row(ticket_id, title=title, body=body,
                       repo=repo, status="processing")
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

    # Pull allowed file paths from AiForgeMemory once — Planner will
    # be constrained to pick from these.
    allowed_files = _fetch_allowed_files(
        repo=repo, query=f"{title}\n{body}", k=80,
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

    # Stage P — Planner (with REPLAN loop on grounder failure)
    p_t0 = time.time()
    plan: dict[str, Any] = {}
    grounding: dict[str, Any] = {}
    plan_attempts = 0
    g_dur = 0.0
    while plan_attempts < 3:
        plan_attempts += 1
        breakers.begin_agent("planner")
        p_agent = registry.build("planner", repo_path=None)
        p_agent.repo = repo; p_agent.ticket_id = ticket_id
        plan = p_agent.run(ctx={
            "understanding": understanding,
            "title": title, "body": body, "repo": repo,
            "allowed_files": allowed_files,
            "skills_hint": skills_hint,
            "failures_hint": failures_hint,
            "previous_plan": plan if plan_attempts > 1 else None,
            "unresolved_refs": grounding.get("unresolved_refs", []),
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
        if grounding.get("resolved") and shrink_ratio < 0.5:
            break
        # Surface dropped refs back to Planner as if they were unresolved.
        if dropped_refs and not grounding.get("unresolved_refs"):
            grounding["unresolved_refs"] = dropped_refs
            grounding["resolved"] = False
    p_dur = time.time() - p_t0 - g_dur  # pure planner time
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

    # Stage V — Verifier (after final plan)
    breakers.begin_agent("verifier")
    v_agent = registry.build("verifier", repo_path=None)
    v_agent.repo = repo; v_agent.ticket_id = ticket_id
    v_t0 = time.time()
    verdict = v_agent.run(ctx={"understanding": understanding, "plan": plan})
    v_dur = time.time() - v_t0
    breakers.check_agent("verifier")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="verifier",
        event_type="agent_completed",
        payload={"verdict": verdict.get("verdict"),
                 "issue_count": len(verdict.get("issues") or [])},
        duration_ms=int(v_dur * 1000),
    )

    # Grounder + REPLAN loop already done above.

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
    if grounding.get("resolved"):
        write_steps = [
            s for s in (plan.get("steps") or [])
            if s.get("action") in ("edit", "create")
        ]
        if not write_steps:
            doer_outcome = {"skipped": True, "reason": "no_write_step"}
        for st in write_steps:
            previous_udiff = ""
            previous_problems: list = []
            for attempt in range(2):  # CRITIC retry per step
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
                # Validator blocked → retry once with feedback
                previous_udiff = step_outcome.get("udiff") or ""
                previous_problems = list(
                    step_outcome.get("problems") or [])
            else:
                # All retries exhausted — keep last outcome
                per_step_outcomes.append({
                    "step": st, "outcome": step_outcome,
                    "validation": step_validation,
                })

        # Aggregate the per-step results into a single doer_outcome /
        # validation pair so downstream stages and metadata stay flat.
        if per_step_outcomes:
            doer_outcome = _aggregate_doer_outcomes(per_step_outcomes)
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
    u_slim = {k: v for k, v in understanding.items() if k != "context_md"}
    _update_ticket_status(ticket_id, final_status, metadata={
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
    }


def _aggregate_doer_outcomes(
    per_step: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge per-step Doer outcomes into one doer_outcome dict.

    Concatenates udiffs (delimited), unions problems, picks last
    branch/applied state. Useful for downstream Validator/Architect
    that still expect a single `doer_outcome`."""
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
    return {
        "artifact_type": "doer_outcome",
        "step_count": len(per_step),
        "target": targets[0] if targets else "",
        "targets": targets,
        "udiff": ("\n\n".join(udiffs))[:8000],
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


def _fetch_allowed_files(*, repo: str, query: str, k: int = 80) -> list[str]:
    """Pull top-K relevant File_v2 paths from AiForgeMemory for this query.
    Vector + fulltext hybrid via translator. Used to constrain Planner output."""
    try:
        import os
        from neo4j import GraphDatabase
        from aiforge_memory.query.translator import (
            _embed_query, _vector_topk, _fulltext_symbols,
        )
    except Exception:
        return []

    try:
        drv = GraphDatabase.driver(
            os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
            auth=(
                os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
                os.environ.get("AIFORGE_NEO4J_PASSWORD", "password"),
            ),
        )
    except Exception:
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
    try:
        # Vector hits
        try:
            vec = _embed_query(query)
            for r in _vector_topk(drv, repo=repo, vec=vec, k=k * 3):
                fp = r.get("file_path")
                if _ok(fp) and fp not in files:
                    files.append(fp)
        except Exception:
            pass

        # Fulltext hits
        try:
            _, ft_files = _fulltext_symbols(drv, repo=repo, text=query, k=k * 3)
            for fp in ft_files:
                if _ok(fp) and fp not in files:
                    files.append(fp)
        except Exception:
            pass

        # Top-N from each Service's CONTAINS_FILE (always include service files)
        try:
            with drv.session() as s:
                rows = list(s.run(
                    "MATCH (:Service {repo:$repo})-[:CONTAINS_FILE]->(f:File_v2) "
                    "RETURN f.path AS p LIMIT $k",
                    repo=repo, k=k * 3,
                ))
            for r in rows:
                p = r["p"]
                if _ok(p) and p not in files:
                    files.append(p)
        except Exception:
            pass
    finally:
        try:
            drv.close()
        except Exception:
            pass

    return files[:k]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-agents-run")
    p.add_argument("--repo", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--ticket-id")
    a = p.parse_args(argv)
    out = run(repo=a.repo, title=a.title, body=a.body, ticket_id=a.ticket_id)
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["circuit_breaker_tripped"] else 1


if __name__ == "__main__":
    sys.exit(main())
