"""Jira connector (standards gap C11 — part 2).

KISS: REST API call to ``$JIRA_BASE_URL/rest/api/3/issue/<KEY>``
with ``JIRA_EMAIL`` + ``JIRA_API_TOKEN`` env-only basic auth. Joins
summary + description (ADF flattened) + top comments into one
markdown blob and hands to the gap-9 ingest spine.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("aiforge.connectors.jira")


def _adf_to_text(node) -> str:
    """Flatten Atlassian Document Format → plain text. Best-effort."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") in {"paragraph", "heading"}:
        return _adf_to_text(node.get("content") or []) + "\n\n"
    if node.get("type") == "hardBreak":
        return "\n"
    if node.get("type") == "bulletList":
        return "\n".join(
            "- " + _adf_to_text(item.get("content") or [])
            for item in (node.get("content") or [])
        ) + "\n"
    return _adf_to_text(node.get("content") or [])


def _fetch(issue_key: str) -> dict | None:
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not (base and email and token):
        return None
    creds = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    url = f"{base}/rest/api/3/issue/{issue_key}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {creds}",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("jira fetch failed: %s", exc)
        return None
    return data


def _render(issue: dict) -> str:
    key = issue.get("key", "?")
    fields = issue.get("fields") or {}
    parts = [
        f"# {key} {fields.get('summary','')}",
        f"Status: {(fields.get('status') or {}).get('name','?')}",
        f"Type: {(fields.get('issuetype') or {}).get('name','?')}",
        "",
        _adf_to_text(fields.get("description") or {}).strip(),
    ]
    comments = ((fields.get("comment") or {}).get("comments") or [])
    if comments:
        parts.append("\n## Comments\n")
        for c in comments[:50]:
            author = (c.get("author") or {}).get("displayName") or "?"
            parts.append(f"- **{author}** ({c.get('created','')}):")
            parts.append(_adf_to_text(c.get("body") or {}).strip())
            parts.append("")
    return "\n".join(parts)


def ingest_jira_issue(
    driver,
    *,
    issue_key: str,
    repo: str,
    tags: list[str] | None = None,
) -> dict:
    """Ingest one Jira issue into AFM. Returns the spine's
    ``{ok, doc_id, note_ids, errors}``."""
    issue = _fetch(issue_key)
    if issue is None:
        return {"ok": False, "error": "missing_env_or_fetch_failed",
                "issue_key": issue_key}
    body = _render(issue)
    from aiforge_memory.features.external_ingest import ingest_external_source
    return ingest_external_source(
        driver,
        source=body,
        repo=repo,
        source_type="jira",
        title=f"{issue_key} {issue.get('fields', {}).get('summary','')}",
        tags=list(tags or []) + [f"jira_key:{issue_key}"],
    )


__all__ = ["ingest_jira_issue"]
