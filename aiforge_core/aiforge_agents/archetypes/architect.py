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
        from aiforge_core.aiforge_agents.runtime import prompt_helpers as ph

        understanding = ctx.get("understanding", {})
        plan = ctx.get("plan", {})
        doer = ctx.get("doer_outcome", {})
        validation = ctx.get("validation", {})
        failures_hint = ctx.get("failures_hint") or []
        u_slim = {k: v for k, v in (understanding or {}).items()
                  if k != "context_md"}
        failures_block = ph.render_failures_block(
            failures_hint,
            header="# Mistakes from prior reviews — flag if seen here:",
        )

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
            f"{failures_block}"
            f"# Understanding\n{u_slim}\n\n# Plan\n{plan}\n\n"
            f"# Diff\n```\n{ph.compact(doer.get('udiff') or '', head=12000, tail=4000)}\n```\n"
        )
        out = llm_client.call_json(
            role=self.name,
            model=self.model or "deepseek-r1-distill-32b",
            system=system, user=user,
            temperature=self.temperature,
            max_tokens=self.max_tokens or 12000,
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

        # Open PR whenever a real diff has been applied to a branch and
        # the caller asked for it. Architect comments still flow back
        # via mr_body — review feedback belongs IN the PR, not as a gate.
        mr_url = ""
        if (ctx.get("open_mr")
                and doer.get("applied")
                and doer.get("applied_branch")
                and decision != "reject"):
            mr_url = _open_github_pr(
                repo_path=ctx.get("repo_path", ""),
                branch=doer.get("applied_branch", ""),
                title=mr_title or "aiforge: code change",
                body=mr_body,
                draft=(decision == "request_changes"),
            )

        return {
            "artifact_type": "review",
            "decision": decision,
            "comments": list(out.get("comments") or []),
            "mr_title":  mr_title,
            "mr_body":   mr_body,
            "mr_url":    mr_url,
        }


_TRANSIENT_GH_PHRASES: tuple[str, ...] = (
    "rate limit", "could not resolve", "connection reset",
    "connection refused", "operation timed out", "timeout",
    "502 bad gateway", "503 service unavailable", "504 gateway",
    "remote end hung up", "temporarily unavailable",
    "i/o operation", "early eof", "ssh_exchange_identification",
)

_PR_EXISTS_PHRASES: tuple[str, ...] = (
    "already exists", "a pull request for branch",
)


def _is_transient_gh_err(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(p in s for p in _TRANSIENT_GH_PHRASES)


def _existing_pr_url(run_fn, branch: str) -> str:
    """If a PR for ``branch`` already exists, return its URL. Else ""."""
    pv = run_fn([
        "gh", "pr", "view", branch, "--json", "url", "-q", ".url",
    ])
    if pv.returncode == 0:
        url = (pv.stdout or "").strip()
        if url.startswith("http"):
            return url
    return ""


def _open_github_pr(
    *, repo_path: str, branch: str, title: str, body: str,
    draft: bool = False,
) -> str:
    """Push branch + invoke `gh pr create`. Returns PR URL or "" on fail.

    Retries `git push` and `gh pr create` independently with exponential
    backoff on transient errors (rate-limit, DNS, 5xx, conn reset). On
    "PR already exists" we look up the existing PR via `gh pr view`
    instead of failing — common when Architect retries on a branch that
    already has an open PR.

    Best-effort: requires `gh` CLI authenticated for the repo, branch
    already committed (Doer apply path does that), and remote=origin.
    Picks up the repo's default branch automatically (master vs main)."""
    import os as _os
    import random as _random
    import subprocess
    import time as _time
    from pathlib import Path

    if not repo_path or not branch or not Path(repo_path).is_dir():
        return ""

    max_attempts = max(1, int(_os.environ.get("AIFORGE_GH_RETRY_MAX", "3")))
    base = float(_os.environ.get("AIFORGE_GH_RETRY_BASE_S", "1.0"))
    cap = float(_os.environ.get("AIFORGE_GH_RETRY_CAP_S", "10.0"))

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
            timeout=60,
        )

    def _backoff(attempt: int) -> float:
        return min(cap, base * (2 ** (attempt - 1))) + _random.uniform(0, 0.3)

    # Determine the actual default branch — falls back to "main".
    base_branch = "main"
    sb = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"])
    if sb.returncode == 0:
        ref = sb.stdout.strip().split("/")[-1]
        if ref:
            base_branch = ref

    # Stage 1: push with retry. Permanent failures (auth, non-fast-forward
    # without --force, no-such-branch) abort immediately.
    push_ok = False
    for attempt in range(1, max_attempts + 1):
        push = _run(["git", "push", "-u", "origin", branch])
        if push.returncode == 0:
            push_ok = True
            break
        if attempt >= max_attempts or not _is_transient_gh_err(push.stderr):
            break
        _time.sleep(_backoff(attempt))
    if not push_ok:
        return ""

    # Stage 2: gh pr create with retry. "Already exists" is a soft success.
    args = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base_branch,
        "--head", branch,
    ]
    if draft:
        args.append("--draft")
    for attempt in range(1, max_attempts + 1):
        pr = _run(args)
        if pr.returncode == 0:
            out = (pr.stdout or "").strip()
            for line in reversed(out.splitlines()):
                if line.startswith("http"):
                    return line
            return out
        stderr_low = (pr.stderr or "").lower()
        if any(p in stderr_low for p in _PR_EXISTS_PHRASES):
            return _existing_pr_url(_run, branch)
        if attempt >= max_attempts or not _is_transient_gh_err(pr.stderr):
            return ""
        _time.sleep(_backoff(attempt))
    return ""
