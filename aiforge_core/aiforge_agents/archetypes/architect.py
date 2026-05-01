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
        decision = str(out.get("decision", "request_changes"))
        mr_title = str(out.get("mr_title", ""))[:70]
        mr_body = str(out.get("mr_body", ""))

        # Optional: actually open the PR via `gh` if approved + branch
        # exists + caller asked for it (ctx['open_mr']=True).
        mr_url = ""
        if (decision == "approve" and ctx.get("open_mr")
                and doer.get("applied")
                and doer.get("applied_branch")):
            mr_url = _open_github_pr(
                repo_path=ctx.get("repo_path", ""),
                branch=doer.get("applied_branch", ""),
                title=mr_title or "aiforge: code change",
                body=mr_body,
            )

        return {
            "artifact_type": "review",
            "decision": decision,
            "comments": list(out.get("comments") or []),
            "mr_title":  mr_title,
            "mr_body":   mr_body,
            "mr_url":    mr_url,
        }


def _open_github_pr(
    *, repo_path: str, branch: str, title: str, body: str,
) -> str:
    """Push branch + invoke `gh pr create`. Returns PR URL or "" on fail.

    Best-effort: requires `gh` CLI authenticated for the repo, branch
    already committed (Doer apply path does that), and remote=origin.
    """
    import subprocess
    from pathlib import Path

    if not repo_path or not branch or not Path(repo_path).is_dir():
        return ""

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
            timeout=60,
        )

    push = _run(["git", "push", "-u", "origin", branch])
    if push.returncode != 0:
        return ""
    pr = _run([
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", "main",
        "--head", branch,
    ])
    if pr.returncode != 0:
        return ""
    return (pr.stdout or "").strip().splitlines()[-1] if pr.stdout else ""
