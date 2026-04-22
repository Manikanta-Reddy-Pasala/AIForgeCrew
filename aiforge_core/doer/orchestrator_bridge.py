"""Bridge between the orchestrator and the smolagents Doer agent.

``run_smolagents_doer`` is called from the graph's doer_node. Runs the
smolagents ToolCallingAgent inside the ticket's isolated git worktree,
then — if the agent returned final_answer AND the diff is non-empty —
commits the change on the per-ticket branch, pushes to origin, and
(where the origin is a GitHub remote) opens a pull request via ``gh``.
Commit/push/PR are all fail-soft: the function returns the Doer result
regardless of whether the publishing steps succeed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from aiforge_core.runtime import tickets
from aiforge_core.runtime.config import DOER_MODEL, LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL
from aiforge_core.runtime.logging_setup import emit

from .agent import build_doer_agent
from .scope_guard import ScopeViolation


class _LLMConfig:
    """Minimal config shim so we don't import the full RoleConfig."""

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key


def _run(cmd: list[str], cwd: str, timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git_commit_push_pr(
    ticket: object,
    worktree_path: str,
    summary_text: str,
    changed_files: list[str],
    log: object,
) -> dict:
    """Commit the diff, push the branch, open a PR if the origin is GitHub.

    Returns a dict with keys: commit_sha, pushed, pr_url — each may be None
    if that step was skipped or failed (fail-soft).
    """
    result = {"commit_sha": None, "pushed": False, "pr_url": None}
    identifier = getattr(ticket, "identifier", "ticket")  # type: ignore[attr-defined]
    title = getattr(ticket, "title", "") or identifier  # type: ignore[attr-defined]
    branch = getattr(ticket, "branch", None)  # type: ignore[attr-defined]

    # 1) commit
    try:
        _run(["git", "add", "-A"], cwd=worktree_path, timeout=30)
        msg = (
            f"aiforge({identifier}): {title}\n\n"
            f"{summary_text[:2000].strip()}\n\n"
            f"Changed files:\n" + "\n".join(f"- {p}" for p in changed_files[:30]) +
            "\n\n"
            "Co-Authored-By: AIForge Doer (smolagents) <noreply@aiforge.local>"
        )
        rc, out, err = _run(
            ["git", "commit", "-m", msg],
            cwd=worktree_path, timeout=60,
        )
        if rc != 0:
            emit(log, "doer.commit.failed", ticket=identifier,
                 err=(err or out)[-200:])
            return result

        sha_rc, sha_out, _ = _run(
            ["git", "rev-parse", "HEAD"], cwd=worktree_path, timeout=10,
        )
        commit_sha = sha_out.strip()[:12] if sha_rc == 0 else None
        result["commit_sha"] = commit_sha
        emit(log, "doer.commit.ok", ticket=identifier, sha=commit_sha,
             files=len(changed_files))
    except Exception as exc:
        emit(log, "doer.commit.exception", ticket=identifier, err=str(exc)[:200])
        return result

    if not branch:
        emit(log, "doer.push.skipped", ticket=identifier, reason="no_branch")
        return result

    # 2) push — fail-soft
    try:
        rc, _, err = _run(
            ["git", "push", "-u", "origin", branch],
            cwd=worktree_path, timeout=180,
        )
        if rc != 0:
            emit(log, "doer.push.failed", ticket=identifier, err=err[-200:])
            return result
        result["pushed"] = True
        emit(log, "doer.push.ok", ticket=identifier, branch=branch)
    except Exception as exc:
        emit(log, "doer.push.exception", ticket=identifier, err=str(exc)[:200])
        return result

    # 3) PR — only if origin is GitHub and gh is installed
    if not shutil.which("gh"):
        emit(log, "doer.pr.skipped", ticket=identifier, reason="gh_missing")
        return result

    rc, origin_url, _ = _run(
        ["git", "remote", "get-url", "origin"], cwd=worktree_path, timeout=10,
    )
    origin_url = origin_url.strip()
    if rc != 0 or not re.search(r"github\.com", origin_url):
        emit(log, "doer.pr.skipped", ticket=identifier,
             reason="origin_not_github", origin=origin_url[:80])
        return result

    try:
        pr_title = f"aiforge({identifier}): {title}"[:120]
        pr_body = (
            f"**Ticket:** {identifier}\n\n"
            f"**Summary:**\n{summary_text[:4000].strip()}\n\n"
            f"**Files:**\n" + "\n".join(f"- `{p}`" for p in changed_files[:30]) +
            f"\n\n---\nAutomated PR by AIForge Doer (smolagents).\n"
            f"Branch: `{branch}`"
        )
        rc, pr_url, err = _run(
            ["gh", "pr", "create",
             "--title", pr_title,
             "--body", pr_body,
             "--head", branch],
            cwd=worktree_path, timeout=120,
        )
        if rc != 0:
            emit(log, "doer.pr.failed", ticket=identifier, err=(err or pr_url)[-300:])
            return result
        result["pr_url"] = pr_url.strip()
        emit(log, "doer.pr.ok", ticket=identifier, url=result["pr_url"])
    except Exception as exc:
        emit(log, "doer.pr.exception", ticket=identifier, err=str(exc)[:200])

    return result


def run_smolagents_doer(ticket: object, worktree_path: str, log: object) -> dict:
    """Run the smolagents ToolCallingAgent for one Doer tick.

    On compile-green + final_answer, commits the diff on the ticket's
    branch, pushes to origin, and opens a GitHub PR (if the origin
    supports it). All publishing steps are fail-soft.
    """
    t_start = time.time()
    ticket_id = ticket.id  # type: ignore[attr-defined]
    role_name = "doer"

    # Build a minimal context bundle.
    try:
        proc = subprocess.run(
            ["/Users/manikanta/.local/bin/aiforge-deep-context",
             ticket.title],  # type: ignore[attr-defined]
            capture_output=True, text=True, timeout=150,
            env={**os.environ, "ROLE": role_name}, check=False,
        )
        context_bundle = proc.stdout or "(deep-context empty)"
    except Exception:
        context_bundle = "(deep-context unavailable — use grep/read_file tools)"

    llm_config = _LLMConfig(
        base_url=LM_STUDIO_BASE_URL,
        model=DOER_MODEL,
        api_key=LM_STUDIO_API_KEY,
    )

    emit(log, "smolagents.start", ticket=ticket.identifier)  # type: ignore[attr-defined]

    try:
        agent, task_prompt = build_doer_agent(ticket, worktree_path, context_bundle, llm_config)
        result = agent.run(task=task_prompt)

        summary_text = str(result) if result is not None else ""

        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=worktree_path, capture_output=True, text=True, check=False,
        )
        changed_files = [ln for ln in diff_proc.stdout.splitlines() if ln.strip()]

        if not changed_files:
            emit(log, "smolagents.no_changes", ticket=ticket.identifier,  # type: ignore[attr-defined]
                 summary_chars=len(summary_text))
            tickets.add_event(
                ticket_id, role_name, "error",
                body=f"final_answer called with empty diff: {summary_text[:2000]}",
                metadata={"stop_reason": "no_changes"},
            )
            return {
                "stop_reason": "no_changes",
                "has_commented": False,
                "turns": getattr(agent, "step_number", 0),
                "wall_s": round(time.time() - t_start, 2),
                "summary": summary_text,
            }

        # Commit + push + PR (fail-soft each step).
        pub = _git_commit_push_pr(ticket, worktree_path, summary_text, changed_files, log)

        event_meta = {
            "source": "smolagents_final_answer",
            "files_changed": changed_files,
            **{k: v for k, v in pub.items() if v is not None},
        }
        comment_body = summary_text[:3500]
        if pub.get("pr_url"):
            comment_body = f"{comment_body}\n\nPR: {pub['pr_url']}"
        elif pub.get("commit_sha"):
            comment_body = f"{comment_body}\n\nCommit: {pub['commit_sha']}"
        tickets.add_event(
            ticket_id, role_name, "comment",
            body=comment_body[:4000],
            metadata=event_meta,
        )
        emit(log, "smolagents.done", ticket=ticket.identifier,  # type: ignore[attr-defined]
             summary_chars=len(summary_text),
             files_changed=len(changed_files),
             commit_sha=pub.get("commit_sha"),
             pushed=pub.get("pushed"),
             pr_url=pub.get("pr_url"))
        return {
            "stop_reason": "final_answer",
            "has_commented": bool(summary_text),
            "turns": getattr(agent, "step_number", 0),
            "wall_s": round(time.time() - t_start, 2),
            "summary": summary_text,
            "commit_sha": pub.get("commit_sha"),
            "pr_url": pub.get("pr_url"),
        }

    except ScopeViolation as exc:
        emit(log, "smolagents.scope_violation",
             ticket=getattr(ticket, "identifier", "?"),
             path=exc.path)
        tickets.add_event(
            ticket_id, role_name, "error",
            body=f"scope violation: {exc}",
            metadata={"stop_reason": "scope_violation"},
        )
        return {
            "stop_reason": "scope_violation",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
        }

    except Exception as exc:
        emit(log, "smolagents.exception",
             ticket=getattr(ticket, "identifier", "?"),
             error=str(exc)[:300])
        tickets.add_event(
            ticket_id, role_name, "error",
            body=f"smolagents exception: {exc}",
            metadata={"stop_reason": "exception"},
        )
        return {
            "stop_reason": "exception",
            "has_commented": False,
            "turns": 0,
            "wall_s": round(time.time() - t_start, 2),
            "summary": str(exc),
        }
