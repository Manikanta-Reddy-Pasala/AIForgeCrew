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
        ticket_id: str | None = None) -> dict[str, Any]:
    ticket_id = ticket_id or f"TKT-{uuid.uuid4().hex[:8].upper()}"
    breakers = cb_mod.CircuitBreakers()
    t0 = time.time()

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
            "previous_plan": plan if plan_attempts > 1 else None,
            "unresolved_refs": grounding.get("unresolved_refs", []),
        })
        breakers.check_agent("planner")

        # Grounder check
        breakers.begin_agent("grounder")
        g_agent = registry.build("grounder", repo_path=None)
        g_agent.repo = repo; g_agent.ticket_id = ticket_id
        g_t0_inner = time.time()
        grounding = g_agent.run(ctx={"plan": plan, "repo": repo})
        g_dur += time.time() - g_t0_inner
        breakers.check_agent("grounder")
        if grounding.get("resolved"):
            break
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

    # Stage D — Doer (only when grounded; otherwise REPLAN per recovery)
    doer_outcome: dict[str, Any] = {"skipped": True, "reason": "not_grounded"}
    d_dur = 0.0
    if grounding.get("resolved"):
        breakers.begin_agent("doer")
        d_agent = registry.build("doer", repo_path=None)
        d_agent.repo = repo; d_agent.ticket_id = ticket_id
        d_t0 = time.time()
        doer_outcome = d_agent.run(ctx={
            "plan": plan, "repo": repo,
            "repo_path": _resolve_repo_path(repo),
            "ticket_id": ticket_id,
        })
        d_dur = time.time() - d_t0
        breakers.check_agent("doer")
        learner.record_audit(
            ticket_id=ticket_id, agent_role="doer",
            event_type="agent_completed",
            payload={"problems": len(doer_outcome.get("problems") or []),
                     "blocked": doer_outcome.get("blocked_by_detectors", False)},
            duration_ms=int(d_dur * 1000),
        )

    # Stage Vd — Validator (basic post-condition check)
    breakers.begin_agent("validator")
    val_agent = registry.build("validator", repo_path=None)
    val_agent.repo = repo; val_agent.ticket_id = ticket_id
    val_t0 = time.time()
    validation = val_agent.run(ctx={"doer_outcome": doer_outcome})
    val_dur = time.time() - val_t0
    breakers.check_agent("validator")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="validator",
        event_type="agent_completed",
        payload={"decision": validation.get("decision"),
                 "checks": validation.get("checks")},
        duration_ms=int(val_dur * 1000),
    )

    # Stage T — Tester (TDD test specs)
    breakers.begin_agent("tester")
    t_agent = registry.build("tester", repo_path=None)
    t_agent.repo = repo; t_agent.ticket_id = ticket_id
    t_t0 = time.time()
    test_plan = t_agent.run(ctx={"understanding": understanding, "plan": plan})
    t_dur = time.time() - t_t0
    breakers.check_agent("tester")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="tester",
        event_type="agent_completed",
        payload={"tests": len(test_plan.get("tests") or [])},
        duration_ms=int(t_dur * 1000),
    )

    # Stage A — Architect (review + MR draft)
    breakers.begin_agent("architect")
    a_agent = registry.build("architect", repo_path=None)
    a_agent.repo = repo; a_agent.ticket_id = ticket_id
    a_t0 = time.time()
    review = a_agent.run(ctx={
        "understanding": understanding, "plan": plan,
        "doer_outcome": doer_outcome, "validation": validation,
    })
    a_dur = time.time() - a_t0
    breakers.check_agent("architect")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="architect",
        event_type="agent_completed",
        payload={"decision": review.get("decision"),
                 "mr_title": review.get("mr_title")},
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

    files: list[str] = []
    try:
        # Vector hits
        try:
            vec = _embed_query(query)
            for r in _vector_topk(drv, repo=repo, vec=vec, k=k):
                fp = r.get("file_path")
                if fp and fp not in files:
                    files.append(fp)
        except Exception:
            pass

        # Fulltext hits
        try:
            _, ft_files = _fulltext_symbols(drv, repo=repo, text=query, k=k)
            for fp in ft_files:
                if fp not in files:
                    files.append(fp)
        except Exception:
            pass

        # Top-N from each Service's CONTAINS_FILE (always include service files)
        try:
            with drv.session() as s:
                rows = list(s.run(
                    "MATCH (:Service {repo:$repo})-[:CONTAINS_FILE]->(f:File_v2) "
                    "RETURN f.path AS p LIMIT $k",
                    repo=repo, k=k,
                ))
            for r in rows:
                p = r["p"]
                if p and p not in files:
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
