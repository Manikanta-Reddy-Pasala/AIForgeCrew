"""Verifier — PreFlect critic on plan + similar past failures."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("verifier")
@dataclass
class Verifier(BaseArchetype):
    name: str = "verifier"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        from aiforge_core.aiforge_agents.runtime import llm_client

        plan = ctx.get("plan", {})
        understanding = ctx.get("understanding", {})

        system = (
            "You are a critic. Given an Understanding and a Plan, decide "
            "if the plan is sound. Output strict JSON: "
            "{verdict, issues[], revised_plan}. "
            "verdict ∈ {pass, repair, reject}. "
            "issues[] = list of {step_id, kind, message}. "
            "If verdict == 'repair', supply revised_plan (same shape as input plan). "
            "Reject if plan references invented files or steps don't address "
            "the Understanding's problem statement."
        )
        user = (
            f"# Understanding\n{understanding}\n\n"
            f"# Plan\n{plan}\n"
        )
        out = llm_client.call_json(
            model=self.model or "qwen2.5-14b-instruct",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 2048,
        )
        if out is None:
            return {"artifact_type": "verifier_verdict",
                    "verdict": "pass", "issues": [], "revised_plan": None,
                    "error": "llm_invalid_json"}
        return {
            "artifact_type": "verifier_verdict",
            "verdict": str(out.get("verdict", "pass")),
            "issues": list(out.get("issues") or []),
            "revised_plan": out.get("revised_plan"),
        }
