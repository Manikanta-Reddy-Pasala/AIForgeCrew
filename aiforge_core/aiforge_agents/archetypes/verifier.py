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

        # Trim heavy fields — context_md can be 4–8k tokens and is not
        # needed for the critic call. Without it max_tokens=2048 was
        # overflowing on big plans, yielding truncated/invalid JSON.
        u_slim = {k: v for k, v in (understanding or {}).items()
                  if k != "context_md"}

        system = (
            "You are a critic. Given an Understanding and a Plan, decide "
            "if the plan is sound. Output STRICT JSON, no prose: "
            "{\"verdict\":\"pass|repair|reject\",\"issues\":[...]}. "
            "issues[] = list of {step_id:int, kind:str, message:str}. "
            "Reject if plan references files explicitly invented "
            "(non-existent under repo root) or steps fail to address the "
            "problem statement. Repair if plan is on track but a step "
            "needs adjustment. Pass if plan is sound. "
            "Do NOT include a revised_plan — orchestrator handles repair "
            "via REPLAN. Keep issues[] short (≤3)."
        )
        user = (
            f"# Understanding\n{u_slim}\n\n"
            f"# Plan\n{plan}\n"
        )
        out = llm_client.call_json(
            model=self.model or "qwen2.5-14b-instruct",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 1024,
        )
        if out is None:
            # Safer fail: invalid JSON should NOT auto-pass. Mark as repair.
            return {"artifact_type": "verifier_verdict",
                    "verdict": "repair", "issues": [
                        {"step_id": 0, "kind": "verifier_error",
                         "message": "verifier returned invalid JSON"},
                    ], "revised_plan": None,
                    "error": "llm_invalid_json"}
        return {
            "artifact_type": "verifier_verdict",
            "verdict": str(out.get("verdict", "pass")),
            "issues": list(out.get("issues") or []),
            "revised_plan": out.get("revised_plan"),
        }
