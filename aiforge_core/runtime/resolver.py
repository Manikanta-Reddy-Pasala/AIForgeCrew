"""Autonomous GitHub-issue resolver (sub #10 follow-up; OH parity).

Polls a GitHub repo for issues tagged ``aiforge-bot`` (configurable)
and converts each into a ticket on the AIForge ticket queue.
The existing :mod:`adk_runner` then processes those tickets normally,
producing a PR via :mod:`git_pr`.

Designed as a thin polling loop — invoke from cron or systemd timer
(``aiforge-resolver.timer``). No webhook server; KISS.

Env knobs:

* ``AIFORGE_RESOLVER_GH_REPO``  — ``owner/repo`` to watch.
* ``AIFORGE_RESOLVER_LABEL``    — issue label to match (default ``aiforge-bot``).
* ``AIFORGE_RESOLVER_INTERVAL`` — seconds between polls when run as loop.
* ``GITHUB_TOKEN``              — auth for the GitHub API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("aiforge.resolver")

_GH_API = "https://api.github.com"
_DEFAULT_LABEL = "aiforge-bot"


def _gh_get(path: str) -> Any:
    token = os.environ.get("GITHUB_TOKEN", "")
    req = urllib.request.Request(
        f"{_GH_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AIForge-Resolver/1.0",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    # Public GitHub API — always full TLS verification (the
    # AIFORGE_LLM_SSL_VERIFY opt-out is scoped to internal hosts only).
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_open_bot_issues(
    repo: str | None = None, label: str | None = None,
) -> list[dict[str, Any]]:
    """Return open GitHub issues on ``repo`` carrying ``label``.

    Soft-error: network failures return an empty list (logged).
    """
    repo = repo or os.environ.get("AIFORGE_RESOLVER_GH_REPO", "")
    label = label or os.environ.get("AIFORGE_RESOLVER_LABEL", _DEFAULT_LABEL)
    if not repo:
        return []
    try:
        path = f"/repos/{repo}/issues?state=open&labels={label}&per_page=20"
        data = _gh_get(path)
    except OSError as exc:
        log.warning("resolver.list_failed repo=%s label=%s: %s",
                    repo, label, exc)
        return []
    # Filter out pull requests — GitHub returns them in /issues.
    return [i for i in data if isinstance(i, dict)
            and i.get("pull_request") is None]


def issue_to_ticket(
    issue: dict[str, Any], project: str = "RESOLVER",
) -> dict[str, Any]:
    """Convert an issue payload into the ticket-store insertion shape."""
    number = issue.get("number") or 0
    title = (issue.get("title") or "").strip()
    body = (issue.get("body") or "").strip()
    user = (issue.get("user") or {}).get("login", "unknown")
    url = issue.get("html_url", "")
    return {
        "project": project,
        "title": title,
        "body": (
            f"{body}\n\n---\n"
            f"Imported from GitHub issue {url} by {user}."
        ),
        "metadata": {
            "source": "github_issue",
            "issue_number": number,
            "issue_url": url,
            "submitter": user,
        },
    }


def _identifier_for(issue_url: str) -> str:
    """A ticket identifier that is the same every poll for the same issue, so
    the store's unique identifier is what stops a second import — including
    after the first ticket is done while the issue is still open. The slug is
    for people; the hash of the full URL keeps ``a-b/c`` and ``a/b-c`` (or two
    hosts) apart. Lowercase, so a slug never looks like a Jira key."""
    path = re.sub(r"^https?://[^/]+/", "", issue_url).replace("/issues/", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")[:60].rstrip("-")
    digest = hashlib.sha1(issue_url.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"GH-{slug}-{digest}"


def resolve_once(
    repo: str | None = None,
    label: str | None = None,
    project: str = "RESOLVER",
) -> dict[str, Any]:
    """Single pass: list open issues, materialise them as tickets.

    Returns ``{ok, scanned, created, skipped_existing, error?}``.
    Skips issues whose ``issue_url`` already exists in the ticket store
    so the same issue isn't re-imported every poll.
    """
    issues = list_open_bot_issues(repo, label)
    scanned = len(issues)
    created = 0
    skipped_existing = 0
    try:
        from aiforge_core.tickets import store as tickets_mod
    except ImportError as exc:
        return {"ok": False, "scanned": scanned,
                "error": f"ticket_store_unavailable: {exc}"}

    for issue in issues:
        ticket = issue_to_ticket(issue, project=project)
        issue_url = ticket["metadata"]["issue_url"]
        if not issue_url:
            continue                  # nothing stable to dedupe on
        ident = _identifier_for(issue_url)
        try:
            if tickets_mod.get(ident) is not None:
                skipped_existing += 1
                continue
            tickets_mod.create(
                project=ticket["project"],
                title=ticket["title"],
                body=ticket["body"],
                metadata=ticket["metadata"],
                identifier=ident,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 — soft per-issue
            log.warning("resolver.create_failed issue=%s: %s",
                        issue_url, exc)

    return {
        "ok": True, "scanned": scanned,
        "created": created, "skipped_existing": skipped_existing,
    }


def loop(
    repo: str | None = None,
    label: str | None = None,
    project: str = "RESOLVER",
    interval_s: int | None = None,
) -> None:  # pragma: no cover — entrypoint, exercised in prod
    """Long-running poll loop. Use under systemd."""
    sleep_s = int(
        interval_s
        or os.environ.get("AIFORGE_RESOLVER_INTERVAL", "60"),
    )
    while True:
        result = resolve_once(repo, label, project)
        log.info("resolver.pass result=%s", result)
        time.sleep(sleep_s)


__all__ = [
    "list_open_bot_issues",
    "issue_to_ticket",
    "resolve_once",
    "loop",
]
