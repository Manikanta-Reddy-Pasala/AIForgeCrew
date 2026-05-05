"""Verifier — pre-execution plan critic. Single LLM completion, no tools.

Runs after Planner emits the plan + child subtickets, BEFORE the
LoopAgent[Doer, Feedback]. Catches bad plans early so a slow Doer turn
isn't wasted on an unreachable spec.

Verdict shape:
    {"verdict": "pass" | "reject", "issues": [...], "rationale": "..."}

A `reject` verdict means the orchestrator should re-plan with the issue
list folded into Planner context. Cap re-plans at 3 per ticket
(orchestrator-level concern, not enforced here).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register


@register("verifier")
@dataclass
class Verifier(BaseArchetype):
    name: str = "verifier"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Single-shot critic over the Planner output.

        Inputs (from ctx):
          plan, child_subtickets, scope_allowlist_globs, parent_ticket,
          repo_index, memory_search_results

        Output: ``{artifact_type: "verifier_verdict", verdict, issues, rationale}``
        """
        from aiforge_core.orchestrator import llm_client

        plan = ctx.get("plan") or {}
        subtickets = ctx.get("child_subtickets") or []
        scope_globs = ctx.get("scope_allowlist_globs") or []

        if not plan or not plan.get("steps"):
            return {
                "artifact_type": "verifier_verdict",
                "verdict": "reject",
                "issues": [{
                    "kind": "missing_plan",
                    "message": "planner emitted empty or missing plan",
                }],
                "rationale": "no plan to verify",
            }

        system = (
            "You are the plan verifier. Critique the plan and return STRICT "
            "JSON only with shape: "
            "{\"verdict\": \"pass\"|\"reject\", "
            "\"issues\": [{\"kind\": \"<kind>\", \"message\": \"<msg>\"}], "
            "\"rationale\": \"<one-line summary>\"}.\n\n"
            "Reject when ANY hold:\n"
            "- A subticket has empty/missing scope_allowlist_globs\n"
            "- A plan step targets paths outside the parent ticket's scope\n"
            "- No test subticket for an acceptance criterion\n"
            "- A plan step references a file/symbol absent from repo_index\n"
            "- The plan exceeds reasonable depth for the ticket size\n"
        )
        user = (
            f"# Plan\n{plan}\n\n"
            f"# Child subtickets\n{subtickets}\n\n"
            f"# Scope allowlist globs\n{scope_globs}\n"
        )
        out = llm_client.call_json(
            role=self.name,
            model=self.model or "claude-opus-4-7",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 1024,
        )
        if out is None:
            return {
                "artifact_type": "verifier_verdict",
                "verdict": "reject",
                "issues": [{"kind": "verifier_error",
                            "message": "verifier returned invalid JSON"}],
                "rationale": "json_decode_failed",
            }

        verdict = out.get("verdict")
        if verdict not in ("pass", "reject"):
            return {
                "artifact_type": "verifier_verdict",
                "verdict": "reject",
                "issues": [{"kind": "verifier_error",
                            "message": f"unknown verdict: {verdict!r}"}],
                "rationale": "bad_verdict_value",
            }
        return {
            "artifact_type": "verifier_verdict",
            "verdict": verdict,
            "issues": list(out.get("issues") or []),
            "rationale": str(out.get("rationale") or ""),
        }
