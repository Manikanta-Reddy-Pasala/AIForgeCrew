"""Understander — read ticket + AiForgeMemory; produce Understanding artifact."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("understander")
@dataclass
class Understander(BaseArchetype):
    name: str = "understander"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        from aiforge_core.aiforge_agents.runtime import llm_client
        from aiforge_core.aiforge_agents.runtime import web_fetch
        from aiforge_core.aiforge_agents.memory import code_context

        title = ctx.get("title", "")
        body  = ctx.get("body", "")
        repo  = ctx.get("repo", self.repo)

        # Pull AiForgeMemory ContextBundle for the ticket text
        try:
            ctx_md = code_context.query(f"{title}\n\n{body}", repo=repo)
        except Exception as exc:
            ctx_md = f"(context query failed: {exc})"

        # Auto-learn from any URLs in the ticket — fetch each page,
        # summarise via LLM, append to context_md as a dedicated section
        # so Planner/Doer/Tester all see the same external knowledge.
        try:
            urls = web_fetch.extract_urls(f"{title}\n{body}")
            if urls:
                ext_md = web_fetch.fetch_and_summarise(
                    urls,
                    ticket_id=ctx.get("ticket_id", self.ticket_id) or "",
                    repo=repo,
                )
                if ext_md:
                    ctx_md = (ctx_md or "") + "\n\n" + ext_md
        except Exception:
            pass

        system = (
            "You analyze a software ticket and produce a structured understanding. "
            "Output strict JSON with fields: problem, knowns[], unknowns[], "
            "risks[], ambiguities[]. Lead with the precise problem statement. "
            "Use the supplied code-graph context to ground knowns + unknowns."
        )
        user = (
            f"# Ticket\n## {title}\n\n{body}\n\n"
            f"# Code-graph context\n{ctx_md}\n"
        )
        out = llm_client.call_json(
            role=self.name,
            model=self.model or "qwen2.5-14b-instruct",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 8000,
        )
        if out is None:
            return {"artifact_type": "understanding",
                    "error": "llm_invalid_json",
                    "problem": title}
        return {
            "artifact_type": "understanding",
            "problem":     str(out.get("problem", "") or title),
            "knowns":      list(out.get("knowns", []) or []),
            "unknowns":    list(out.get("unknowns", []) or []),
            "risks":       list(out.get("risks", []) or []),
            "ambiguities": list(out.get("ambiguities", []) or []),
            "context_md":  ctx_md,
        }
