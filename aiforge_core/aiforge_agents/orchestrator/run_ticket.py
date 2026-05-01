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


def run(*, repo: str, title: str, body: str,
        ticket_id: str | None = None) -> dict[str, Any]:
    ticket_id = ticket_id or f"TKT-{uuid.uuid4().hex[:8].upper()}"
    breakers = cb_mod.CircuitBreakers()
    t0 = time.time()

    learner.migrate()
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

    total = time.time() - t0
    learner.record_episodic(
        ticket_id=ticket_id, stage="plan", agent_role="orchestrator",
        outcome="ok" if not breakers.tripped else "tripped",
        summary=f"P1 loop: U={u_dur:.1f}s P={p_dur:.1f}s total={total:.1f}s",
        artifacts={"understanding": understanding, "plan": plan},
    )

    return {
        "ticket_id": ticket_id,
        "repo": repo,
        "title": title,
        "understanding": understanding,
        "plan": plan,
        "latency_s": round(total, 2),
        "stages": {
            "understander_s": round(u_dur, 2),
            "planner_s":      round(p_dur, 2),
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
