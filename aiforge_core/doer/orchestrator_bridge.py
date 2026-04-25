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
    """Resolve which Doer execution backend to use.

    Priority:
      1. ``AIFORGE_DOER_BACKEND`` env var — explicit override (``smolagents``,
         ``genericagent``, ``code``, ``toolcalling``).  ``genericagent``
         routes through ``run_doer_via_ga``.  Anything else is treated as
         a smolagents agent-class flag (the existing behaviour).
      2. ``agents.yaml`` ``doer.identity.backend`` — falls through here when
         the env var is unset.  ``genericagent_text_protocol`` selects GA.
      3. Default: ``code`` (smolagents CodeAgent).
    """
    env = os.environ.get("AIFORGE_DOER_BACKEND", "").strip().lower()
    if env:
        return env
    try:
        from aiforge_core.agents import load_agents
        backend = (load_agents()["doer"].identity or {}).get("backend", "")
        if backend == "genericagent_text_protocol":
            return "genericagent"
    except Exception:
        pass
    return "code"


def run_smolagents_doer(
    ticket: object,
    worktree_path: str,
    log: object,
    prior_verdict: str | None = None,
    prior_fixlist: str | None = None,
) -> dict:
    """Run one Doer tick. Despite the name, dispatches to the configured
    backend (smolagents CodeAgent, smolagents ToolCallingAgent, or
    GenericAgent text-protocol) based on ``AIFORGE_DOER_BACKEND`` /
    ``agents.yaml``.

    On compile-green + final_answer, commits the diff on the ticket's
    branch, pushes to origin, and opens a GitHub PR (if the origin
    supports it). All publishing steps are fail-soft.

    *prior_verdict* / *prior_fixlist* come from the previous feedback tick and
    are forwarded into the agent's task prompt so Doer can continue from
    where the last tick left off.
    """
    backend = _doer_backend()
    if backend == "genericagent":
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

    emit(log, "doer.backend.selected", backend="smolagents",
         ticket=getattr(ticket, "identifier", "?"))
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
            compile_err = counters.get("last_compile_error", "")
            err_block = f"\n\nLast compile error:\n{compile_err}" if compile_err else ""
            tickets.add_event(
                ticket_id, role_name, "error",
                body=(f"final_answer called but checklist not met "
                      f"(edit_block_ok={edits_ok} < required={min_edits}, "
                      f"compile_green={compile_ok}).\n\n"
                      f"Agent summary: {summary_text[:1500]}{err_block}"),
                metadata={"stop_reason": "checklist_fail", "counters": counters,
                          "required_items": required_items,
                          "compile_error": compile_err[:1500] if compile_err else ""},
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
