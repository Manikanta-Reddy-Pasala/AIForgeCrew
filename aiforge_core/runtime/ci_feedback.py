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
from typing import Any, Callable

log = logging.getLogger("aiforge.ci_feedback")

_RED_CONCLUSIONS = {"failure", "timed_out", "cancelled"}
_LOG_EXCERPT_CAP = 2048  # ~2KB

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _parse_pr_url(url: str) -> tuple[str, str, str] | None:
    m = _PR_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def _gh_json(args: list, *, timeout: int, fail: str, bad_json: str):
    """(data, error). One place for the run / returncode / parse triple that
    was written out three times, each with its own error label."""
    try:
        proc = subprocess.run(["gh", "api", *args], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, {"ok": False, "error": "timeout"}
    if proc.returncode != 0:
        return None, {"ok": False, "error": fail, "stderr": proc.stderr[-400:]}
    try:
        return json.loads(proc.stdout), None
    except json.JSONDecodeError:
        return None, {"ok": False, "error": bad_json}


def _aggregate(runs: list) -> "tuple[str, bool]":
    """(status, completed) across the check runs — red beats green, and
    anything still running beats both."""
    statuses = [r.get("conclusion") for r in runs]
    has_pending = any(r.get("status") != "completed" for r in runs)
    if has_pending:
        return "pending", False
    if any(s in {"failure", "timed_out", "cancelled"} for s in statuses):
        return "red", True
    if statuses and all(s in {"success", "neutral", "skipped"} for s in statuses):
        return "green", True
    return "unknown", True


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

    pr, err = _gh_json([f"repos/{owner}/{repo}/pulls/{num}"],
                       timeout=20, fail="gh_failed", bad_json="bad_json")
    if err:
        return err
    head_sha = (pr.get("head") or {}).get("sha")
    if not head_sha:
        return {"ok": False, "error": "no_head_sha"}

    body, err = _gh_json(
        [f"repos/{owner}/{repo}/commits/{head_sha}/check-runs", "--paginate"],
        timeout=30, fail="gh_checks_failed", bad_json="bad_checks_json")
    if err:
        return err
    runs = body.get("check_runs") or []
    agg, completed = _aggregate(runs)
    return {
        "ok": True,
        "status": agg,
        "completed": completed,
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
    # Carry pr_url + repo forward — on_ci_red / build_fix_request read them off
    # this dict, but read_pr_checks (`snap`) never sets them, so the CI-autofix
    # request was built with empty pr/repo.
    out = {"ok": True, **snap, "rolled_back": False, "pr_url": pr_url}
    _parsed = _parse_pr_url(pr_url)
    if _parsed:
        out["repo"] = f"{_parsed[0]}/{_parsed[1]}"
    if snap.get("status") == "red" and auto_rollback:
        rb = open_revert_pr(pr_url)
        out["rolled_back"] = bool(rb.get("ok"))
        out["rollback_result"] = rb
    return out


def build_fix_request(
    pr: str, repo: str, failed_checks: list[dict]
) -> dict[str, Any]:
    """A3: build a closed-loop follow-up fix request from red checks.

    Pure function — no side effects. ``failed_checks`` is the subset of
    check-run dicts (``{name, conclusion, summary}``) that went red.
    Returns ``{kind, pr, repo, title, body, checks}`` where ``checks``
    is the list of failing check names and ``body`` summarises them
    with any captured log excerpt (truncated to ~2KB total).
    """
    names = [c.get("name") or "(unnamed)" for c in failed_checks]
    title = "CI red — auto-fix: " + ", ".join(names[:5])
    if len(names) > 5:
        title += f" (+{len(names) - 5} more)"

    lines = [
        f"Automated follow-up: CI failed on PR {pr} ({repo}).",
        "Fix the failing checks below.",
        "",
        "Failing checks:",
    ]
    budget = _LOG_EXCERPT_CAP
    for c in failed_checks:
        name = c.get("name") or "(unnamed)"
        excerpt = (c.get("summary") or "").strip()
        if budget <= 0:
            excerpt = ""
        elif len(excerpt) > budget:
            excerpt = excerpt[:budget] + "…(truncated)"
        budget -= len(excerpt)
        if excerpt:
            lines.append(f"- {name}: {excerpt}")
        else:
            lines.append(f"- {name}")
    body = "\n".join(lines)
    return {
        "kind": "ci_fix",
        "pr": pr,
        "repo": repo,
        "title": title,
        "body": body,
        "checks": names,
    }


def on_ci_red(
    graded: dict[str, Any],
    *,
    dispatch: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any] | None:
    """A3: close the CI loop by dispatching a fix request on red.

    Builds a :func:`build_fix_request` from the failing checks in a
    ``grade_and_react`` result. When ``AIFORGE_CI_AUTOFIX_ENABLED`` ==
    ``"1"`` and there is at least one failing check, the fix request is
    handed to the injected ``dispatch`` callable (a new ticket / re-
    dispatch signal). Dependency injection keeps this testable without
    GitHub.

    Returns the fix request dict (regardless of whether it was
    dispatched) so callers can inspect it; returns ``None`` when there
    are no failing checks. Does not replace the existing revert/escalate
    path — it complements it.
    """
    checks = graded.get("checks") or []
    failed = [c for c in checks if c.get("conclusion") in _RED_CONCLUSIONS]
    if not failed:
        return None
    pr = graded.get("pr_url") or graded.get("pr") or ""
    repo = graded.get("repo") or ""
    req = build_fix_request(pr, repo, failed)
    enabled = os.environ.get("AIFORGE_CI_AUTOFIX_ENABLED", "0") == "1"
    if enabled and dispatch is not None:
        try:
            dispatch(req)
        except Exception:  # noqa: BLE001 - dispatch failures must not break CI grading
            log.exception("ci autofix dispatch failed for %s", pr)
    return req


__all__ = [
    "read_pr_checks",
    "open_revert_pr",
    "grade_and_react",
    "build_fix_request",
    "on_ci_red",
]
