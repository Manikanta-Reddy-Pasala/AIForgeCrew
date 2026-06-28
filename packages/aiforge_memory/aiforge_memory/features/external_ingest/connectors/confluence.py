"""Confluence connector (gap M5 — part 1).

KISS: REST API GET to
``$CONFLUENCE_BASE_URL/rest/api/content/<id>?expand=body.storage``
with ``CONFLUENCE_EMAIL`` + ``CONFLUENCE_TOKEN`` env-only basic auth.
Strips the storage-format HTML to plain text, joins title + body into
one markdown blob and hands it to the gap-9 ingest spine.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

from aiforge_memory.features.external_ingest import ingest_external_source

log = logging.getLogger("aiforge.connectors.confluence")


def _strip_html(html: str) -> str:
    """Best-effort storage-format HTML → plain text."""
    if not html:
        return ""
    # Block-ish tags become paragraph breaks, the rest are dropped.
    text = re.sub(r"</(p|h[1-6]|li|tr|div|br)\s*>", "\n\n", html,
                  flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fetch(page_id: str) -> dict | None:
    base = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_TOKEN", "")
    if not (base and email and token):
        return None
    creds = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    url = f"{base}/rest/api/content/{page_id}?expand=body.storage"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {creds}",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("confluence fetch failed: %s", exc)
        return None
    return data


def _render(page: dict) -> str:
    title = page.get("title", "")
    storage = (((page.get("body") or {}).get("storage") or {}).get("value")) or ""
    parts = [f"# {title}", "", _strip_html(storage)]
    return "\n".join(parts).strip()


def ingest_confluence_page(
    driver,
    *,
    page_id: str,
    repo: str,
    tags: list[str] | None = None,
) -> dict:
    """Ingest one Confluence page into AFM. Returns the spine's
    ``{ok, doc_id, note_ids, errors}`` shape, or
    ``{ok: False, error: ...}`` when creds are missing / fetch fails."""
    page = _fetch(page_id)
    if page is None:
        return {"ok": False, "error": "missing_env_or_fetch_failed",
                "page_id": page_id}
    body = _render(page)
    return ingest_external_source(
        driver,
        source=body,
        repo=repo,
        source_type="confluence",
        title=page.get("title", "") or f"confluence:{page_id}",
        tags=list(tags or []) + [f"confluence_id:{page_id}"],
    )


__all__ = ["ingest_confluence_page"]
