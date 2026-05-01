"""Architect — read-only review; opens MR if approved."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("architect")
@dataclass
class Architect(BaseArchetype):
    name: str = "architect"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Read-only review of Doer's diff vs Plan + Understanding.
        Decision: approve | request_changes | reject.
        If approve AND ctx['open_mr']=True AND a branch+diff is ready,
        invoke `gh pr create`. Else just emit MR title + body."""
        from aiforge_core.aiforge_agents.runtime import llm_client

        understanding = ctx.get("understanding", {})
        plan = ctx.get("plan", {})
        doer = ctx.get("doer_outcome", {})
        validation = ctx.get("validation", {})

        if validation.get("decision") != "approve":
            return {"artifact_type": "review",
                    "decision": "request_changes",
                    "comments": [f"validation blocked: {validation.get('reason')}"],
                    "mr_title": "", "mr_body": "",
                    "mr_url": ""}

        system = (
            "You are a read-only architect. Review the diff against "
            "Understanding + Plan. Output strict JSON: "
            "{decision, comments[], mr_title, mr_body}. "
            "decision ∈ {approve, request_changes, reject}. "
            "mr_title ≤ 70 chars. mr_body in markdown w/ ## Summary, ## Changes, ## Tests."
        )
        user = (
            f"# Understanding\n{understanding}\n\n# Plan\n{plan}\n\n"
            f"# Diff\n```\n{(doer.get('udiff') or '')[:3000]}\n```\n"
        )
        out = llm_client.call_json(
            model=self.model or "deepseek-r1-distill-32b",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 3000,
        )
        if out is None:
            return {"artifact_type": "review",
                    "decision": "request_changes",
                    "comments": ["llm_invalid_json"],
                    "mr_title": "", "mr_body": "",
                    "mr_url": ""}
        return {
            "artifact_type": "review",
            "decision": str(out.get("decision", "request_changes")),
            "comments": list(out.get("comments") or []),
            "mr_title":  str(out.get("mr_title", ""))[:70],
            "mr_body":   str(out.get("mr_body",  "")),
            "mr_url":    "",  # actual gh pr create deferred
        }
