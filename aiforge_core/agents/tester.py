"""Tester — TDD-first; writes failing tests before Doer iterates."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.agents.base import BaseArchetype
from aiforge_core.agents.registry import register

@register("tester")
@dataclass
class Tester(BaseArchetype):
    name: str = "tester"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Generate failing-test cases from Plan + Understanding.
        Output JSON list of test specs: {name, target_class, target_method,
        scenario, expected, framework}."""
        from aiforge_core.orchestrator import llm_client
        from aiforge_core.orchestrator import prompt_helpers as ph

        understanding = ctx.get("understanding", {})
        plan = ctx.get("plan", {})
        failures_hint = ctx.get("failures_hint") or []

        # Strip context_md and compact whatever's left so the small
        # local model has room for the test list itself.
        u_slim = {k: v for k, v in (understanding or {}).items()
                  if k != "context_md"}
        failures_block = ph.render_failures_block(
            failures_hint,
            header="# Mistakes from prior tickets — write tests covering these:",
        )

        system = (
            "You write failing test specs for a code-change ticket. "
            "Output strict JSON: {tests:[{name, target_class, target_method, "
            "scenario, expected, framework}], coverage_target}. "
            "framework ∈ {junit5, pytest, jest, mockito}. "
            "Each test must target a real class/method from the Plan. "
            "Tests should fail BEFORE the change is applied (TDD)."
        )
        user = (
            f"{failures_block}"
            f"# Understanding\n{u_slim}\n\n# Plan\n{plan}\n"
        )
        out = llm_client.call_json(
            role=self.name,
            model=self.model or "qwen2.5-coder-14b",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 12000,
        )
        if out is None:
            return {"artifact_type": "test_plan",
                    "tests": [], "coverage_target": 0.8,
                    "error": "llm_invalid_json"}
        try:
            cov = float(out.get("coverage_target", 0.8))
        except (TypeError, ValueError):
            cov = 0.8
        return {
            "artifact_type": "test_plan",
            "tests": list(out.get("tests") or []),
            "coverage_target": cov,
        }
