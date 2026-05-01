"""Learner — ADK after_model + after_tool callback. Online + offline modes."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("learner")
@dataclass
class Learner(BaseArchetype):
    name: str = "learner"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Online learner — distil one episodic + procedural row from
        the run's artifacts, write to Postgres. No LLM yet — heuristic.
        Skill promotion is offline (separate cron, P2).
        """
        from aiforge_core.aiforge_agents.learner import online

        ticket_id = ctx.get("ticket_id", self.ticket_id)
        repo = ctx.get("repo", self.repo)
        plan = ctx.get("plan") or {}
        verdict = ctx.get("verifier_verdict") or {}
        grounding = ctx.get("grounding") or {}
        doer_out = ctx.get("doer_outcome") or {}
        validation = ctx.get("validation") or {}

        # task_class = first action of plan, used for procedural pattern key
        steps = plan.get("steps") or []
        task_class = (
            (doer_out.get("target") or "")
            .split("/")[-2]   # second-to-last path segment ≈ feature dir
            if doer_out.get("target") else "unknown"
        ) or "unknown"

        tool_sequence = [s.get("action", "") for s in steps if s.get("action")]
        success = (
            verdict.get("verdict") == "pass"
            and grounding.get("resolved", False)
            and validation.get("decision") == "approve"
        )

        outcome = "success" if success else (
            "blocked" if grounding.get("unresolved_refs")
            else "rejected"
        )
        summary = (
            f"plan_steps={len(steps)} verdict={verdict.get('verdict','?')} "
            f"grounded={grounding.get('resolved',False)} "
            f"validation={validation.get('decision','?')} "
            f"detectors={len(doer_out.get('problems') or [])}"
        )

        artifacts = {
            "plan_steps":     len(steps),
            "verdict":        verdict.get("verdict"),
            "grounded":       grounding.get("resolved"),
            "unresolved":     len(grounding.get("unresolved_refs") or []),
            "doer_problems":  len(doer_out.get("problems") or []),
            "validation":     validation.get("decision"),
        }

        online.record_episodic(
            ticket_id=ticket_id, stage="full_run", agent_role="learner",
            outcome=outcome, summary=summary, artifacts=artifacts,
        )
        online.update_procedural(
            agent_role="planner", task_class=task_class,
            tool_sequence=tool_sequence, success=success,
        )

        return {
            "artifact_type": "learning",
            "outcome": outcome,
            "task_class": task_class,
            "tool_sequence": tool_sequence,
            "summary": summary,
        }
