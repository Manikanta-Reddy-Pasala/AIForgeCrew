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

        # Order-aware: targets created by earlier steps in this same
        # plan are valid for later steps' read|edit|test references.
        created_so_far: set[str] = set()

        def _norm(p: str) -> str:
            return p[p.find("src/"):] if "/src/" in p else p

        try:
            with drv.session() as s:
                for st in steps:
                    tgt = (st.get("target") or "").strip()
                    action = (st.get("action") or "").lower()
                    if not tgt or action in {"search", "run"}:
                        continue
                    tgt_norm = _norm(tgt)

                    # `create` action: target is a NEW file. Walk
                    # parent dirs until one exists in the graph. Allows
                    # fresh feature packages under existing source roots
                    # (e.g. new `feature/storeregion/` under `feature/`)
                    # AND fresh test packages under `src/test/java/` even
                    # when the target test subdir has no current files.
                    if action == "create":
                        parts = tgt_norm.split("/")
                        if len(parts) < 2:
                            # top-level files (README.md etc.) auto-pass
                            created_so_far.add(tgt_norm)
                            continue
                        # Try progressively shallower ancestor dirs.
                        ancestors = [
                            "/".join(parts[:i]) + "/"
                            for i in range(len(parts) - 1, 0, -1)
                        ]
                        # Any ancestor with at least one indexed file
                        # in the same repo counts as resolvable parent.
                        row = s.run(
                            "UNWIND $prefixes AS prefix "
                            "MATCH (f:File_v2 {repo: $repo}) "
                            "WHERE f.path STARTS WITH prefix "
                            "RETURN prefix LIMIT 1",
                            repo=repo, prefixes=ancestors,
                        ).single()
                        if row is None:
                            unresolved.append({
                                "step_id": st.get("id"),
                                "target": tgt,
                                "action": action,
                                "reason": "parent_dir_missing",
                            })
                        else:
                            created_so_far.add(tgt_norm)
                        continue

                    # read|edit|test: file must exist (or be created
                    # earlier in this same plan).
                    if tgt_norm in created_so_far:
                        continue
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
