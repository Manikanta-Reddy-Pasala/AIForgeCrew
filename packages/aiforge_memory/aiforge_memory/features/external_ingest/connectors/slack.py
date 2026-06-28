"""Slack connector (gap M5 — part 2).

KISS: REST API GET to
``https://slack.com/api/conversations.history?channel=<id>`` with a
``SLACK_TOKEN`` env-only bearer token. Joins the recent messages into
one markdown transcript and hands it to the gap-9 ingest spine.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from aiforge_memory.features.external_ingest import ingest_external_source

log = logging.getLogger("aiforge.connectors.slack")

_HISTORY_LIMIT = 200


def _fetch(channel: str) -> dict | None:
    token = os.environ.get("SLACK_TOKEN")
    if not token:
        return None
    qs = urllib.parse.urlencode({"channel": channel, "limit": _HISTORY_LIMIT})
    url = f"https://slack.com/api/conversations.history?{qs}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("slack fetch failed: %s", exc)
        return None
    if not data or not data.get("ok"):
        log.debug("slack api error: %s", (data or {}).get("error"))
        return None
    return data


def _render(channel: str, history: dict) -> str:
    parts = [f"# Slack #{channel}", ""]
    # Slack returns newest-first; render oldest-first for readability.
    messages = list(reversed(history.get("messages") or []))
    for m in messages:
        user = m.get("user") or m.get("username") or "?"
        ts = m.get("ts", "")
        text = (m.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"- **{user}** ({ts}): {text}")
    return "\n".join(parts).strip()


def ingest_slack_channel(
    driver,
    *,
    channel: str,
    repo: str,
    tags: list[str] | None = None,
) -> dict:
    """Ingest a Slack channel's recent history into AFM. Returns the
    spine's ``{ok, doc_id, note_ids, errors}`` shape, or
    ``{ok: False, error: ...}`` when ``SLACK_TOKEN`` is missing / the
    fetch fails."""
    history = _fetch(channel)
    if history is None:
        return {"ok": False, "error": "missing_token_or_fetch_failed",
                "channel": channel}
    body = _render(channel, history)
    return ingest_external_source(
        driver,
        source=body,
        repo=repo,
        source_type="slack",
        title=f"slack:{channel}",
        tags=list(tags or []) + [f"slack_channel:{channel}"],
    )


__all__ = ["ingest_slack_channel"]
