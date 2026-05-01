"""Planner — six-layer hardened plan generation."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("planner")
@dataclass
class Planner(BaseArchetype):
    name: str = "planner"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        from aiforge_core.aiforge_agents.runtime import llm_client
        from aiforge_core.aiforge_agents.runtime import detectors

        understanding = ctx.get("understanding", {})
        ctx_md = understanding.get("context_md", "")

        system = (
            "You generate a plan to satisfy the ticket. Output strict JSON "
            "with fields: steps (array), expected_token_budget (int). "
            "Each step has: id, action (one of read|edit|test|run), "
            "target (file path or symbol), inputs, expected (post-condition), "
            "depends_on (array of step ids). Max 7 steps. "
            "Steps must reference real files/symbols from the code-graph "
            "context — never invent paths."
        )
        user = (
            f"# Understanding\n{understanding}\n\n"
            f"# Code-graph context\n{ctx_md}\n"
        )
        out = llm_client.call_json(
            model=self.model or "deepseek-r1-distill-32b",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 6000,
        )
        if out is None:
            return {"artifact_type": "plan",
                    "error": "llm_invalid_json",
                    "steps": []}

        plan = {
            "artifact_type": "plan",
            "steps": list(out.get("steps", []) or []),
            "expected_token_budget": int(out.get("expected_token_budget", 0) or 0),
            "from_skill": None,
        }
        # F-006 depth check
        depth_hit = detectors.check_plan_depth(plan)
        if depth_hit is not None:
            plan["depth_violation"] = depth_hit.evidence
        return plan
