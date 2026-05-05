"""Planner — six-layer hardened plan generation."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register

@register("planner")
@dataclass
class Planner(BaseArchetype):
    name: str = "planner"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        from aiforge_core.orchestrator import llm_client
        from aiforge_core.orchestrator import detectors
        from aiforge_core.orchestrator import prompt_helpers as ph

        understanding = ctx.get("understanding", {})
        ctx_md = understanding.get("context_md", "")
        # Compact heavy context_md. With a 128K-token window we can
        # afford generous head/tail; just enough trimming to keep the
        # output budget unmolested.
        ctx_md = ph.compact(ctx_md, head=4000, tail=2000)
        allowed_files = ctx.get("allowed_files") or []
        skills_hint = ctx.get("skills_hint") or []
        failures_hint = ctx.get("failures_hint") or []
        previous_plan = ctx.get("previous_plan") or {}
        unresolved   = ctx.get("unresolved_refs") or []

        # Strict allowlist seeding the prompt — Planner must pick from here
        # OR explicitly mark step as `action: create` for a NEW file with
        # a path that follows the repo's package convention.
        allowed_block = ""
        if allowed_files:
            # Show top 40 to the model — full 80 still flow through the
            # post-Planner allowlist filter. 80 lines × 60 chars was
            # crowding small-model context window and pushing the JSON
            # output toward truncation.
            allowed_block = (
                "# Allowed file paths (use ONLY these for action=read|edit|test|run; "
                "for action=create, use a path that matches one of these directories)\n"
                + "\n".join(f"- {p}" for p in allowed_files[:80])
                + "\n"
            )
        failures_block = ph.render_failures_block(failures_hint)

        skills_block = ""
        if skills_hint:
            skill_lines = ["# Skills (recipes that worked on similar tasks here):"]
            for s in skills_hint[:3]:
                wins = s.get("success_count", 0)
                losses = s.get("failure_count", 0)
                skill_lines.append(
                    f"- **{s.get('name','?')}** "
                    f"(✓{wins}/✗{losses}): {s.get('summary','')}"
                )
            skills_block = "\n".join(skill_lines) + "\n"

        replan_block = ""
        if previous_plan and unresolved:
            replan_block = (
                "# Previous attempt was BLOCKED by Grounder — these refs did not resolve.\n"
                "# Replace them with real paths from `# Allowed file paths` above, "
                "or change action=create with a real package path.\n"
                + "\n".join(
                    f"- step {u.get('step_id')}: target was `{u.get('target')}` "
                    f"(action={u.get('action')})"
                    for u in unresolved
                ) + "\n"
            )

        system = (
            "You generate a plan to satisfy the ticket. Output strict JSON "
            "with fields: steps (array), expected_token_budget (int). "
            "Each step has: id, action (read|edit|test|run|create), "
            "target (file path or symbol), inputs, expected (post-condition), "
            "depends_on (array of step ids). Max 12 steps — use them.\n"
            "\n"
            "GRANULARITY: this system runs on a small local model. Prefer "
            "MANY SMALL steps over a few big ones — each `create` step "
            "should produce ONE file (entity OR controller OR service OR "
            "repository OR test), not multiple files at once. Splitting a "
            "feature into 5 single-file create steps is BETTER than 2 "
            "multi-file create steps; each step is then a single bounded "
            "LLM call which fits in our token budget.\n"
            "\n"
            "STRICT RULE: every read|edit|test|run target MUST appear in "
            "the allowed file list. For action=create, target must be a "
            "NEW path whose parent directory matches a package shown in "
            "the allowed list. Never invent arbitrary paths.\n"
            "\n"
            "EDIT-INTENT RULE: If the ticket title or body uses any of these words about\n"
            "EXISTING files — \"edit\", \"modify\", \"add to\", \"update\", \"refactor\", \"fix\",\n"
            "\"change\", \"remove from\", \"replace in\" — every step in your plan MUST use\n"
            "action=edit or action=create. NEVER emit action=read on its own as a\n"
            "standalone step for an edit-intent ticket. The Doer can read while editing.\n"
            "A plan with all action=read steps blocks the pipeline (Doer is skipped),\n"
            "which counts as a planning failure.\n"
            "\n"
            "When in doubt, prefer action=edit. The post-Planner allowlist filter and\n"
            "the Verifier will catch invalid targets."
        )
        user = (
            f"# Understanding\n{understanding}\n\n"
            f"{failures_block}"
            f"{skills_block}"
            f"{allowed_block}"
            f"{replan_block}"
            f"# Code-graph context\n{ctx_md}\n"
        )
        out = llm_client.call_json(
            role=self.name,
            model=self.model or "deepseek-r1-distill-32b",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 24000,
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
