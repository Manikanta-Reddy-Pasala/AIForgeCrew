"""Doer — CRITIC loop with five layered checks."""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import Any

from aiforge_core.aiforge_agents.base import BaseArchetype
from aiforge_core.aiforge_agents.registry import register

@register("doer")
@dataclass
class Doer(BaseArchetype):
    name: str = "doer"

    def run(self, *, ctx: dict[str, Any]) -> dict[str, Any]:
        """Single-pass Doer:
            1. pick first 'edit' step from plan
            2. read target file via repo path
            3. ask LLM for unified diff (grammar nudged via prompt)
            4. extract imports → HallucinatedImportDetector
            5. parse hunks → DiffContextHashDetector
            6. write a `.aiforge/proposals/<ticket>.patch` artifact
            7. (optional) apply + git diff verify if ctx['apply']=True

        No CRITIC retry loop yet — that's P2.
        """
        from pathlib import Path

        from aiforge_core.aiforge_agents.runtime import detectors, llm_client

        plan = ctx.get("plan", {})
        repo = ctx.get("repo", self.repo)
        repo_path = ctx.get("repo_path", "")
        ticket_id = ctx.get("ticket_id", self.ticket_id)
        apply = bool(ctx.get("apply", False))

        # Pick first write step (edit OR create — both produce code)
        write_steps = [s for s in (plan.get("steps") or [])
                       if s.get("action") in ("edit", "create")]
        if not write_steps:
            return {"artifact_type": "doer_outcome",
                    "skipped": True, "reason": "no_write_step"}
        step = write_steps[0]
        target_rel = step.get("target", "")
        action = step.get("action", "edit")

        # Read target file for `edit`; for `create`, file_text is empty
        # and we ask LLM to generate the full file.
        file_text = ""
        if action == "edit" and repo_path and target_rel:
            try:
                file_text = (Path(repo_path) / target_rel).read_text()
            except OSError as exc:
                return {"artifact_type": "doer_outcome",
                        "skipped": True,
                        "reason": f"target_unreadable: {exc}",
                        "step_id": step.get("id"),
                        "target": target_rel}

        # LLM — generate udiff
        if action == "create":
            system = (
                "You produce a unified diff (udiff) that CREATES a new file. "
                "Output ONE fenced block: ```diff\\n--- /dev/null\\n+++ b/<path>\\n"
                "@@ -0,0 +1,<count> @@\\n+<line>\\n+<line>\\n...\\n``` "
                "Every line of the new file is a `+` line. "
                "Use only imports valid for the project's package conventions. "
                "Match the source language of the target path."
            )
            user = (
                f"# Step (create new file)\n{step}\n\n"
                f"# Target path: {target_rel}\n"
                f"# Plan context\n{plan.get('steps')}\n"
            )
        else:
            system = (
                "You produce a unified diff (udiff) that satisfies the step. "
                "Output ONE fenced block: ```diff\\n--- a/<path>\\n+++ b/<path>\\n"
                "@@ -<start>,<count> +<start>,<count> @@\\n<context/+/->...\\n``` "
                "Context lines (starting with single space) MUST exactly match "
                "lines in the supplied source. Never invent imports — only use "
                "imports already present in the file or its package. "
                "Keep diff minimal."
            )
            user = (
                f"# Step\n{step}\n\n"
                f"# Target file: {target_rel}\n"
                f"```\n{file_text[:10_000]}\n```\n"
            )
        raw = llm_client.call_text(
            model=self.model or "qwen3-coder-next",
            system=system, user=user,
            temperature=self.temperature or 0.2,
            max_tokens=self.max_tokens or 4000,
        )
        # Extract diff fenced block
        import re
        m = re.search(r"```diff\s*\n(.*?)```", raw, re.DOTALL)
        udiff = m.group(1) if m else raw

        # Detectors
        problems: list[dict] = []

        imp_det = detectors.HallucinatedImportDetector(repo=repo, driver=None)
        for hit in imp_det.check(udiff):
            problems.append({"mode": hit.mode.id, "evidence": hit.evidence})

        # Diff hash check only valid for `edit` (existing file content)
        if action == "edit" and file_text:
            hash_hit = detectors.DiffContextHashDetector.check(
                udiff=udiff, file_text=file_text,
            )
            if hash_hit:
                problems.append({"mode": hash_hit.mode.id,
                                 "evidence": hash_hit.evidence})

        # Save proposal artifact (always)
        artifact_path = ""
        if repo_path and ticket_id:
            try:
                proposals = Path(repo_path) / ".aiforge" / "proposals"
                proposals.mkdir(parents=True, exist_ok=True)
                artifact_path = str(proposals / f"{ticket_id}.patch")
                Path(artifact_path).write_text(udiff)
            except OSError:
                artifact_path = ""

        return {
            "artifact_type": "doer_outcome",
            "step_id": step.get("id"),
            "action": action,
            "target": target_rel,
            "udiff": udiff[:4000],   # truncate for storage
            "problems": problems,
            "applied": False,        # apply path (P2) not wired
            "tests_green": False,    # tests path (P2) not wired
            "artifact_path": artifact_path,
            "blocked_by_detectors": len(problems) > 0,
        }
