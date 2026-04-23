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


_REQ_HEADER_RX = re.compile(
    r"^\s*##+\s*(?:acceptance\s*criteria|required|requirements|must(?:\s+do)?|done\s+when)\b",
    re.I | re.M,
)
_NUMBERED_ITEM_RX = re.compile(r"^\s*(?:\d+\.|\*|-)\s+\*?\*?([^\n]{3,})", re.M)


def _count_required_items(body: str) -> int:
    """Best-effort count of acceptance criteria bullets in the ticket body.

    Looks for a section header like ``## Acceptance criteria`` / ``## Required``
    and counts numbered/bulleted items under it. Falls back to a generic scan
    over the whole body if no header is present. Returns 0 when the ticket
    looks like a plain prose request.
    """
    if not body:
        return 0
    m = _REQ_HEADER_RX.search(body)
    if m:
        tail = body[m.end():]
        # Stop at the next ## section if any.
        end = re.search(r"\n##+\s", tail)
        block = tail[: end.start()] if end else tail
        return len(_NUMBERED_ITEM_RX.findall(block))
    # No explicit section — if the body has a single numbered list, count it.
    items = _NUMBERED_ITEM_RX.findall(body)
    return len(items) if len(items) >= 2 else 0


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

    # 2) push — use --force-with-lease so retries after a blocked tick don't
    #    get rejected as non-fast-forward against their own earlier push.
    try:
        rc, _, err = _run(
            ["git", "push", "--force-with-lease", "-u", "origin", branch],
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


def run_smolagents_doer(
    ticket: object,
    worktree_path: str,
    log: object,
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> dict:
    """Run the smolagents ToolCallingAgent for one Doer tick.

    On compile-green + final_answer, commits the diff on the ticket's
    branch, pushes to origin, and opens a GitHub PR (if the origin
    supports it). All publishing steps are fail-soft.

    *prior_verdict* / *prior_fixlist* come from the previous feedback tick and
    are forwarded into the agent's task prompt so Doer can continue from
    where the last tick left off.
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

    # Count required acceptance items from the ticket body so we can gate
    # final_answer on "N edit_blocks green", not just ">=1". A ticket with
    # "Acceptance criteria: 1. X  2. Y  3. Z" should produce >=3 edit_blocks.
    required_items = _count_required_items(getattr(ticket, "body", "") or "")

    try:
        counters: dict = {"edit_block_ok": 0, "compile_green": 0}
        agent, task_prompt = build_doer_agent(
            ticket, worktree_path, context_bundle, llm_config, counters=counters,
            prior_verdict=prior_verdict, prior_fixlist=prior_fixlist,
        )
        # Tell the agent upfront how many distinct edits we expect.
        if required_items > 1:
            task_prompt += (
                f"\n\n## Required-items bar\n"
                f"The ticket lists ~{required_items} acceptance items. "
                f"Do NOT call final_answer until you have made at least "
                f"{required_items} successful edit_block calls AND "
                f"run_compile returns EXIT=0.\n"
            )
        result = agent.run(task=task_prompt)

        summary_text = str(result) if result is not None else ""

        # Programmatic checklist enforcement.
        edits_ok = counters.get("edit_block_ok", 0)
        compile_ok = counters.get("compile_green", 0)
        min_edits = max(1, required_items)
        checklist_failed = (edits_ok < min_edits) or (compile_ok == 0)
        if checklist_failed:
            emit(log, "smolagents.checklist_fail", ticket=ticket.identifier,  # type: ignore[attr-defined]
                 summary_chars=len(summary_text),
                 counters=counters, required_items=required_items)
            tickets.add_event(
                ticket_id, role_name, "error",
                body=(f"final_answer called but checklist not met "
                      f"(edit_block_ok={edits_ok} < required={min_edits}, "
                      f"compile_green={compile_ok}).\n\n"
                      f"Agent summary: {summary_text[:1500]}"),
                metadata={"stop_reason": "checklist_fail", "counters": counters,
                          "required_items": required_items},
            )
            # Preserve the worktree diff — the feedback→doer retry should
            # continue from this state instead of starting from pristine
            # (2026-04-23 ONE-16 finding). Only wipe on terminal cleanup.
            return {
                "stop_reason": "checklist_fail",
                "has_commented": False,
                "turns": getattr(agent, "step_number", 0),
                "wall_s": round(time.time() - t_start, 2),
                "summary": summary_text,
            }

        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=worktree_path, capture_output=True, text=True, check=False,
        )
        all_changed = [ln for ln in diff_proc.stdout.splitlines() if ln.strip()]

        # Filter build noise that gets written by mvn compile itself
        # (flattened-pom, target/, .mvn cache, etc). Require at least one
        # real source file change to count as "work done".
        _NOISE_PATTERNS = (
            ".flattened-pom.xml", "target/", ".mvn/", ".idea/",
            ".gradle/", "node_modules/", ".aiforge-worktrees/",
        )
        real_changes = [
            p for p in all_changed
            if not any(p == n or p.startswith(n) for n in _NOISE_PATTERNS)
        ]

        if not real_changes:
            emit(log, "smolagents.no_real_changes", ticket=ticket.identifier,  # type: ignore[attr-defined]
                 summary_chars=len(summary_text),
                 noise_only=all_changed)
            tickets.add_event(
                ticket_id, role_name, "error",
                body=(f"final_answer called but only build-noise files changed "
                      f"({all_changed}); no source edit detected.\n\n"
                      f"Agent summary: {summary_text[:1500]}"),
                metadata={"stop_reason": "no_changes", "noise_only": all_changed},
            )
            # Undo the noise so the worktree is clean for the next attempt.
            subprocess.run(["git", "checkout", "--", "."],
                           cwd=worktree_path, check=False,
                           capture_output=True, timeout=30)
            return {
                "stop_reason": "no_changes",
                "has_commented": False,
                "turns": getattr(agent, "step_number", 0),
                "wall_s": round(time.time() - t_start, 2),
                "summary": summary_text,
            }

        changed_files = real_changes

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
