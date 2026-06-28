"""Linear connector (standards gap C11 — part 1).

KISS: GraphQL POST to ``https://api.linear.app/graphql`` with
``LINEAR_API_KEY`` env-only auth. Fetches issue title + description +
top comments, joins into one markdown blob, hands to the gap-9
ingest spine.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("aiforge.connectors.linear")

_LINEAR_QUERY = """
query($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    state { name }
    comments(first: 50) {
      nodes { user { name } body createdAt }
    }
  }
}
"""


def _fetch(issue_id: str) -> dict | None:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        return None
    body = json.dumps({"query": _LINEAR_QUERY, "variables": {"id": issue_id}})
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=body.encode("utf-8"),
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("linear fetch failed: %s", exc)
        return None
    return ((data or {}).get("data") or {}).get("issue")


def _render(issue: dict) -> str:
    parts = [
        f"# {issue.get('identifier')} {issue.get('title','')}",
        f"State: {(issue.get('state') or {}).get('name','?')}",
        "",
        issue.get("description") or "",
    ]
    comments = (issue.get("comments") or {}).get("nodes") or []
    if comments:
        parts.append("\n## Comments\n")
        for c in comments[:50]:
            author = (c.get("user") or {}).get("name") or "?"
            parts.append(f"- **{author}** ({c.get('createdAt','')}):")
            parts.append((c.get("body") or "").strip())
            parts.append("")
    return "\n".join(parts)


def ingest_linear_issue(
    driver,
    *,
    issue_id: str,
    repo: str,
    tags: list[str] | None = None,
) -> dict:
    """Ingest one Linear issue into AFM. Returns the ingest spine's
    ``{ok, doc_id, note_ids, errors}`` shape, or
    ``{ok: False, error: "missing_api_key"}`` when ``LINEAR_API_KEY``
    isn't set."""
    issue = _fetch(issue_id)
    if issue is None:
        return {"ok": False, "error": "missing_api_key_or_fetch_failed",
                "issue_id": issue_id}
    body = _render(issue)
    from aiforge_memory.features.external_ingest import ingest_external_source
    return ingest_external_source(
        driver,
        source=body,
        repo=repo,
        source_type="linear",
        title=f"{issue.get('identifier')} {issue.get('title','')}",
        tags=list(tags or []) + [f"linear_id:{issue_id}"],
    )


__all__ = ["ingest_linear_issue"]
