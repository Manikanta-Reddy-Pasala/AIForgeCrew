"""Grounder — validate every plan reference resolves before exec."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("grounder")
@dataclass
class Grounder(BaseArchetype):
    name: str = "grounder"

    # Grounder is rule-based; uses no LLM. Override default to "".
    def __post_init__(self) -> None:
        self.model = ""

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Rule-based: each plan step's `target` must exist in
        the AiForgeMemory File_v2 graph for this repo. No LLM."""
        import os
        from neo4j import GraphDatabase

        plan = ctx.get("plan", {})
        repo = ctx.get("repo", self.repo)
        steps = plan.get("steps") or []

        unresolved: list[dict[str, Any]] = []
        try:
            drv = GraphDatabase.driver(
                os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687"),
                auth=(
                    os.environ.get("AIFORGE_NEO4J_USER", "neo4j"),
                    os.environ.get("AIFORGE_NEO4J_PASSWORD", "password"),
                ),
            )
        except Exception as exc:
            return {"artifact_type": "grounding",
                    "resolved": False,
                    "unresolved_refs": [],
                    "error": f"neo4j_driver: {exc}"}

        try:
            with drv.session() as s:
                for st in steps:
                    tgt = (st.get("target") or "").strip()
                    action = (st.get("action") or "").lower()
                    if not tgt or action in {"search", "run"}:
                        continue
                    # Strip worktree prefix if any
                    if "/src/" in tgt:
                        tgt_norm = tgt[tgt.find("src/"):]
                    else:
                        tgt_norm = tgt

                    # `create` action: target is a NEW file. Validate
                    # only that the parent directory exists (any file
                    # currently inside that dir is OK).
                    if action == "create":
                        parent = "/".join(tgt_norm.split("/")[:-1])
                        if not parent:
                            continue
                        row = s.run(
                            "MATCH (f:File_v2 {repo: $repo}) "
                            "WHERE f.path STARTS WITH $prefix "
                            "RETURN f.path AS p LIMIT 1",
                            repo=repo, prefix=parent + "/",
                        ).single()
                        if row is None:
                            unresolved.append({
                                "step_id": st.get("id"),
                                "target": tgt,
                                "action": action,
                                "reason": "parent_dir_missing",
                            })
                        continue

                    # read|edit|test: file must exist
                    row = s.run(
                        "MATCH (f:File_v2 {repo: $repo}) "
                        "WHERE f.path = $p OR f.path ENDS WITH $suffix "
                        "RETURN f.path AS p LIMIT 1",
                        repo=repo, p=tgt_norm,
                        suffix="/" + tgt_norm.split("/")[-1],
                    ).single()
                    if row is None:
                        unresolved.append({
                            "step_id": st.get("id"),
                            "target": tgt,
                            "action": action,
                            "reason": "file_missing",
                        })
        finally:
            try:
                drv.close()
            except Exception:
                pass

        return {"artifact_type": "grounding",
                "resolved": len(unresolved) == 0,
                "unresolved_refs": unresolved}
