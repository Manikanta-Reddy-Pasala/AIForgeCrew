"""Bridge between the orchestrator and the GA Doer agent.

``run_smolagents_doer`` is called from the graph's doer_node. Despite
the historical name, it dispatches solely to the GenericAgent (GA)
backend — smolagents has been removed. On compile-green + final_answer,
commits the diff on the ticket's branch, pushes to origin, and opens a
GitHub PR (if the origin supports it). All publishing steps are
fail-soft: the function returns the Doer result regardless of whether
the publishing steps succeed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from aiforge_core.runtime import tickets
from aiforge_core.runtime.logging_setup import emit

from .scope_guard import ScopeViolation


# Match the first block that looks like an explicit acceptance contract.
# Supports both ``## Acceptance criteria`` headers and inline prose such as
# ``Acceptance criteria — ALL three must be implemented:``.
_REQ_HEADER_RX = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?"
    r"(?:acceptance\s*criteria|required|requirements|must\s+do|done\s+when)"
    r"\b[^\n]{0,120}",
    re.I,
)
# Only count top-level numbered items — ``1. ...``. Bullet lists are
# explicitly ignored because planner output adds lots of sub-bullets under
# each numbered step, which would inflate the count.
_NUMBERED_ITEM_RX = re.compile(r"^\s{0,3}(\d+)\.\s+\S", re.M)

_ACCEPTANCE_CAP = 8  # hard cap — no real ticket has more than ~5 acceptance items


def _count_required_items(body: str) -> int:
    """Count numbered acceptance items from the first contract block.

    Looks for the first ``Acceptance criteria`` / ``Required`` line, then
    counts numbered list items (``1.`` / ``2.`` / …) until the block ends
    — defined as the first blank line that's followed by a new section
    (``## ...``) or by non-item prose.
    Returns 0 when the ticket has no such block. Capped at ``_ACCEPTANCE_CAP``.
    """
    if not body:
        return 0
    m = _REQ_HEADER_RX.search(body)
    if not m:
        return 0
    tail = body[m.end():]
    # Terminate at the next markdown section header or a double-newline that
    # introduces something other than a numbered item.
    end = re.search(r"\n\s*#{1,6}\s+\w", tail)
    block = tail[: end.start()] if end else tail
    # Also trim at the first blank line followed by non-numbered prose.
    lines = block.splitlines()
    collected: list[str] = []
    in_list = False
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            if in_list:
                # blank line after we started the list ends it unless the next
                # non-empty line continues numbering; approximate by stopping.
                break
            continue
        if _NUMBERED_ITEM_RX.match(ln):
            in_list = True
            collected.append(stripped)
        elif in_list and not stripped[0].isdigit():
            # prose between items — stop collecting once we're past the list
            if len(collected) >= 1:
                break
    return min(len(collected), _ACCEPTANCE_CAP)


def _run(cmd: list[str], cwd: str, timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


_TITLE_PREFIX_RX = re.compile(
    r"^\s*\[(?:V\d+[-_].*?|EVAL[-_].*?|ADK[-_].*?|GA[-_].*?|"
    r"FULL[-_].*?|FINAL[-_].*?|RETRY[-_].*?|TEST[-_].*?|"
    r"GO[-_]LIVE.*?|SMOKE.*?|TMP.*?|WIP.*?)\]\s*",
    re.IGNORECASE,
)


def _clean_pr_title(raw: str) -> str:
    """Strip eval prefixes like [V5-ADK-FINAL], [EVAL-...] etc.

    Tickets get throwaway tags during testing. The PR title should be
    the actual change description, not the eval label.
    """
    if not raw:
        return ""
    s = raw.strip()
    while True:
        m = _TITLE_PREFIX_RX.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    return s.strip() or raw.strip()


def _extract_acceptance(body: str) -> str:
    """Pull the ## Acceptance criteria block from the ticket body.

    Returns the section content trimmed; falls back to "_(see ticket
    body)_" when no header found. Caps at 1500 chars so PRs stay
    skimmable.
    """
    if not body:
        return "_(see ticket body)_"
    if "\\n" in body and "\n" not in body:
        body = body.replace("\\n", "\n")
    lower = body.lower()
    headers = ("## acceptance criteria", "## acceptance")
    for h in headers:
        idx = lower.find(h)
        if idx < 0:
            continue
        nl = body.find("\n", idx)
        if nl < 0:
            continue
        section = body[nl + 1:]
        end = section.find("\n## ")
        if end >= 0:
            section = section[:end]
        section = section.strip()
        if section:
            return section[:1500]
    return "_(see ticket body)_"


def _git_commit_only(
    ticket: object,
    worktree_path: str,
    changed_files: list[str],
    log: object,
) -> dict:
    """Commit local diff into the worktree branch only — no push, no PR.

    Returns ``{"commit_sha": <12-char hex or None>}``. Used by the GA
    runner so the feedback gate has a chance to veto before publishing.
    """
    result = {"commit_sha": None}
    identifier = getattr(ticket, "identifier", "ticket")  # type: ignore[attr-defined]
    raw_title = getattr(ticket, "title", "") or identifier  # type: ignore[attr-defined]
    title = _clean_pr_title(raw_title)
    try:
        _run(["git", "add", "-A"], cwd=worktree_path, timeout=30)
        msg = (
            f"{title}\n\n"
            f"Ticket: {identifier}\n\n"
            f"Files changed:\n" + "\n".join(f"- {p}" for p in changed_files[:30]) +
            "\n"
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


def _git_push_pr(
    ticket: object,
    worktree_path: str,
    summary_text: str,
    changed_files: list[str],
    log: object,
) -> dict:
    """Push the already-committed branch and open a PR (GitHub only).

    Returns ``{"pushed": bool, "pr_url": str|None}``. Idempotent — caller
    only invokes this after the feedback gate verdict is pass. Skipping
    the commit step (it's already in the prior commit on HEAD).
    """
    result = {"pushed": False, "pr_url": None}
    identifier = getattr(ticket, "identifier", "ticket")  # type: ignore[attr-defined]
    raw_title = getattr(ticket, "title", "") or identifier  # type: ignore[attr-defined]
    branch = getattr(ticket, "branch", None)  # type: ignore[attr-defined]
    body = getattr(ticket, "body", "") or ""  # type: ignore[attr-defined]
    title = _clean_pr_title(raw_title)
    acceptance_block = _extract_acceptance(body)

    if not branch:
        emit(log, "doer.push.skipped", ticket=identifier, reason="no_branch")
        return result
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
        pr_title = f"{identifier}: {title}"[:120]
        files_block = "\n".join(f"- `{p}`" for p in changed_files[:30]) or "_(no files)_"
        pr_body = (
            f"## Ticket\n{identifier}\n\n"
            f"## What\n{title}\n\n"
            f"## Files changed\n{files_block}\n\n"
            f"## Acceptance criteria\n{acceptance_block}\n\n"
            f"---\n"
            f"Automated PR by AIForgeCrew. Branch: `{branch}`"
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


def _git_commit_push_pr(
    ticket: object,
    worktree_path: str,
    summary_text: str,
    changed_files: list[str],
    log: object,
) -> dict:
    """Legacy: commit + push + PR atomically. Kept for callers that
    still want the all-in-one path. New code should use
    ``_git_commit_only`` followed by ``_git_push_pr`` so the feedback
    gate can veto between commit and publish.
    """
    result = {"commit_sha": None, "pushed": False, "pr_url": None}
    identifier = getattr(ticket, "identifier", "ticket")  # type: ignore[attr-defined]
    raw_title = getattr(ticket, "title", "") or identifier  # type: ignore[attr-defined]
    branch = getattr(ticket, "branch", None)  # type: ignore[attr-defined]
    body = getattr(ticket, "body", "") or ""  # type: ignore[attr-defined]

    title = _clean_pr_title(raw_title)
    acceptance_block = _extract_acceptance(body)

    # 1) commit
    try:
        _run(["git", "add", "-A"], cwd=worktree_path, timeout=30)
        msg = (
            f"{title}\n\n"
            f"Ticket: {identifier}\n\n"
            f"Files changed:\n" + "\n".join(f"- {p}" for p in changed_files[:30]) +
            "\n"
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
        pr_title = f"{identifier}: {title}"[:120]
        files_block = "\n".join(f"- `{p}`" for p in changed_files[:30]) or "_(no files)_"
        pr_body = (
            f"## Ticket\n{identifier}\n\n"
            f"## What\n{title}\n\n"
            f"## Files changed\n{files_block}\n\n"
            f"## Acceptance criteria\n{acceptance_block}\n\n"
            f"---\n"
            f"Automated PR by AIForgeCrew. Branch: `{branch}`"
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


def _doer_backend() -> str:
    """Stub kept for compatibility — always returns ``genericagent``."""
    return "genericagent"


def run_smolagents_doer(
    ticket: object,
    worktree_path: str,
    log: object,
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> dict:
    """Run one Doer tick — dispatches solely to the GA runner.

    The historical name is preserved so existing callers (adk_workflow.py
    et al.) don't need updating. Smolagents has been removed; this
    function always routes through ``run_doer_via_ga``.

    *prior_verdict* / *prior_fixlist* come from the previous feedback tick and
    are forwarded into the agent's task prompt so Doer can continue from
    where the last tick left off.
    """
    from .ga_runner import run_doer_via_ga
    # Pack the prior fixlist into the plan_text so GA sees it without
    # forking the prompt builder.
    plan_bits = []
    if prior_verdict == "fail" and (prior_fixlist or "").strip():
        plan_bits.append(
            "## Previous feedback (fix list)\n" + prior_fixlist.strip()
        )
    plan_text = "\n\n".join(plan_bits)
    max_turns = int(os.environ.get("AIFORGE_DOER_MAX_TURNS", "30"))
    emit(log, "doer.backend.selected", backend="genericagent",
         ticket=getattr(ticket, "identifier", "?"))
    return run_doer_via_ga(
        ticket, worktree_path, plan_text=plan_text,
        max_turns=max_turns, log=log,
    )
