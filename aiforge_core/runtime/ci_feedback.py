"""CI feedback ingest (standards gap C1) + auto-rollback (C10).

After ``commit_push_open_pr`` returns a PR URL the runner shouldn't
hand the world back to humans — read the PR's check-runs via ``gh
api`` and grade them. When CI is red and the ticket was a doc-only /
trivial change, open a revert PR instead of leaving a broken merge
candidate (C10).

KISS: ``gh`` CLI subprocess; no GitHub SDK; no webhooks. Soft-fail
when ``gh`` missing or rate-limited.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

log = logging.getLogger("aiforge.ci_feedback")

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _parse_pr_url(url: str) -> tuple[str, str, str] | None:
    m = _PR_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def read_pr_checks(pr_url: str) -> dict[str, Any]:
    """Return the PR's current check-run summary.

    ``{ok, status, conclusion, checks: [{name, conclusion, summary}],
    completed: bool, raw_count}``. ``status`` aggregates to ``red`` /
    ``green`` / ``pending`` / ``unknown``.

    Soft-failures: ``gh`` missing → ``{ok: False,
    error: "missing_gh"}``; auth failure → bubbles in ``error``.
    """
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return {"ok": False, "error": "bad_pr_url", "pr_url": pr_url}
    owner, repo, num = parsed
    if shutil.which("gh") is None:
        return {"ok": False, "error": "missing_gh"}
    try:
        proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{owner}/{repo}/commits/pull/{num}/check-runs"
                if False else
                f"repos/{owner}/{repo}/pulls/{num}",
            ],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    if proc.returncode != 0:
        return {"ok": False, "error": "gh_failed",
                "stderr": proc.stderr[-400:]}
    try:
        pr = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json"}
    head_sha = pr.get("head", {}).get("sha")
    if not head_sha:
        return {"ok": False, "error": "no_head_sha"}
    proc2 = subprocess.run(
        ["gh", "api",
         f"repos/{owner}/{repo}/commits/{head_sha}/check-runs",
         "--paginate"],
        capture_output=True, text=True, timeout=30,
    )
    if proc2.returncode != 0:
        return {"ok": False, "error": "gh_checks_failed",
                "stderr": proc2.stderr[-400:]}
    try:
        body = json.loads(proc2.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_checks_json"}
    runs = body.get("check_runs") or []
    statuses = [r.get("conclusion") for r in runs]
    has_pending = any(r.get("status") not in {"completed"} for r in runs)
    if has_pending:
        agg = "pending"
    elif any(s in {"failure", "timed_out", "cancelled"} for s in statuses):
        agg = "red"
    elif statuses and all(s in {"success", "neutral", "skipped"} for s in statuses):
        agg = "green"
    else:
        agg = "unknown"
    return {
        "ok": True,
        "status": agg,
        "completed": not has_pending,
        "raw_count": len(runs),
        "checks": [
            {
                "name": r.get("name"),
                "conclusion": r.get("conclusion"),
                "summary": (r.get("output") or {}).get("summary", "")[:300],
            }
            for r in runs[:20]
        ],
    }


def open_revert_pr(pr_url: str) -> dict[str, Any]:
    """C10: open a `gh pr revert`-style PR that reverts the merge.

    Real impl: the merge hasn't happened yet (CI failed pre-merge), so
    we don't auto-revert. Instead we close the PR with a comment so the
    branch doesn't sit half-merged. Operator can re-open after fixing.
    """
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        return {"ok": False, "error": "bad_pr_url"}
    owner, repo, num = parsed
    if shutil.which("gh") is None:
        return {"ok": False, "error": "missing_gh"}
    msg = (
        "**aiforge:auto-rollback** CI red on this PR — closing so the "
        "branch isn't merged accidentally. Re-open after fixing the "
        "checks; the worktree branch is preserved."
    )
    proc = subprocess.run(
        ["gh", "pr", "comment", num, "--repo", f"{owner}/{repo}",
         "--body", msg],
        capture_output=True, text=True, timeout=20,
    )
    out = {"comment_ok": proc.returncode == 0,
           "stderr": proc.stderr[-200:]}
    close = subprocess.run(
        ["gh", "pr", "close", num, "--repo", f"{owner}/{repo}",
         "--comment", "aiforge:auto-rollback closed PR (see prior comment)"],
        capture_output=True, text=True, timeout=20,
    )
    out["close_ok"] = close.returncode == 0
    out["ok"] = out["comment_ok"] and out["close_ok"]
    return out


def grade_and_react(
    pr_url: str,
    *,
    auto_rollback: bool | None = None,
    poll_seconds: int = 0,
) -> dict[str, Any]:
    """Read PR checks, optionally roll back on red.

    ``poll_seconds > 0`` waits that long for at least one check to
    appear before grading — useful when called immediately after a
    push. Hard cap 120 s to avoid blocking the runner.

    ``auto_rollback`` defaults to ``AIFORGE_CI_AUTO_ROLLBACK`` env
    (off by default — safer than silently closing PRs).
    """
    if auto_rollback is None:
        auto_rollback = os.environ.get("AIFORGE_CI_AUTO_ROLLBACK", "0") in {"1", "true"}
    deadline = time.time() + max(0, min(int(poll_seconds), 120))
    while True:
        snap = read_pr_checks(pr_url)
        if snap.get("ok") and snap.get("raw_count", 0) > 0:
            break
        if time.time() >= deadline:
            break
        time.sleep(5)
    if not snap.get("ok"):
        return snap
    out = {"ok": True, **snap, "rolled_back": False}
    if snap.get("status") == "red" and auto_rollback:
        rb = open_revert_pr(pr_url)
        out["rolled_back"] = bool(rb.get("ok"))
        out["rollback_result"] = rb
    return out


__all__ = ["read_pr_checks", "open_revert_pr", "grade_and_react"]
