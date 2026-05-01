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

    # Stage P — Planner
    breakers.begin_agent("planner")
    p_agent = registry.build("planner", repo_path=None)
    p_agent.repo = repo
    p_agent.ticket_id = ticket_id
    p_t0 = time.time()
    plan = p_agent.run(ctx={"understanding": understanding,
                            "title": title, "body": body, "repo": repo})
    p_dur = time.time() - p_t0
    breakers.check_agent("planner")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="planner",
        event_type="agent_completed",
        payload={"steps": len(plan.get("steps") or [])},
        duration_ms=int(p_dur * 1000),
    )

    # Stage V — Verifier
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

    # Stage G — Grounder (rule-based, queries Neo4j)
    breakers.begin_agent("grounder")
    g_agent = registry.build("grounder", repo_path=None)
    g_agent.repo = repo; g_agent.ticket_id = ticket_id
    g_t0 = time.time()
    grounding = g_agent.run(ctx={"plan": plan, "repo": repo})
    g_dur = time.time() - g_t0
    breakers.check_agent("grounder")
    learner.record_audit(
        ticket_id=ticket_id, agent_role="grounder",
        event_type="agent_completed",
        payload={"resolved": grounding.get("resolved"),
                 "unresolved_count": len(grounding.get("unresolved_refs") or [])},
        duration_ms=int(g_dur * 1000),
    )

    total = time.time() - t0
    learner.record_episodic(
        ticket_id=ticket_id, stage="plan", agent_role="orchestrator",
        outcome="ok" if not breakers.tripped else "tripped",
        summary=f"P1 loop: U={u_dur:.1f}s P={p_dur:.1f}s total={total:.1f}s",
        artifacts={"understanding": understanding, "plan": plan},
    )

    final_status = (
        "failed" if breakers.tripped
        else ("blocked" if not grounding.get("resolved") else "done")
    )
    _update_ticket_status(ticket_id, final_status, metadata={
        "runtime": "aiforge_agents",
        "stages_s": {
            "understander": round(u_dur, 2),
            "planner":      round(p_dur, 2),
            "verifier":     round(v_dur, 2),
            "grounder":     round(g_dur, 2),
        },
        "latency_s": round(total, 2),
        "verdict": verdict.get("verdict"),
        "grounded": grounding.get("resolved"),
        "unresolved_refs": len(grounding.get("unresolved_refs") or []),
    })

    return {
        "ticket_id": ticket_id,
        "repo": repo,
        "title": title,
        "understanding": understanding,
        "plan": plan,
        "verifier_verdict": verdict,
        "grounding": grounding,
        "latency_s": round(total, 2),
        "stages": {
            "understander_s": round(u_dur, 2),
            "planner_s":      round(p_dur, 2),
            "verifier_s":     round(v_dur, 2),
            "grounder_s":     round(g_dur, 2),
        },
        "circuit_breaker_tripped": breakers.tripped,
        "circuit_breaker_reason":  breakers.state.reason,
    }


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
