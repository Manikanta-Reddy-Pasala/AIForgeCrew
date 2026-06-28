"""Notion connector (gap M5 — part 3).

KISS: REST API GET to
``https://api.notion.com/v1/blocks/<page_id>/children`` with a
``NOTION_TOKEN`` env-only bearer token. Flattens the block rich-text
into one markdown blob and hands it to the gap-9 ingest spine.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from aiforge_memory.features.external_ingest import ingest_external_source

log = logging.getLogger("aiforge.connectors.notion")

_NOTION_VERSION = "2022-06-28"

# block type → markdown prefix
_PREFIX = {
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "bulleted_list_item": "- ",
    "numbered_list_item": "- ",
    "to_do": "- ",
    "quote": "> ",
}


def _fetch(page_id: str) -> dict | None:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return None
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": _NOTION_VERSION,
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("notion fetch failed: %s", exc)
        return None
    return data


def _rich_text(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in (rich_text or []))


def _render(page_id: str, blocks: dict) -> str:
    parts: list[str] = []
    for block in blocks.get("results") or []:
        btype = block.get("type")
        payload = block.get(btype) or {}
        text = _rich_text(payload.get("rich_text"))
        if not text:
            continue
        parts.append(f"{_PREFIX.get(btype, '')}{text}")
    return "\n\n".join(parts).strip()


def ingest_notion_page(
    driver,
    *,
    page_id: str,
    repo: str,
    tags: list[str] | None = None,
) -> dict:
    """Ingest one Notion page's blocks into AFM. Returns the spine's
    ``{ok, doc_id, note_ids, errors}`` shape, or
    ``{ok: False, error: ...}`` when ``NOTION_TOKEN`` is missing / the
    fetch fails."""
    blocks = _fetch(page_id)
    if blocks is None:
        return {"ok": False, "error": "missing_token_or_fetch_failed",
                "page_id": page_id}
    body = _render(page_id, blocks)
    return ingest_external_source(
        driver,
        source=body,
        repo=repo,
        source_type="notion",
        title=f"notion:{page_id}",
        tags=list(tags or []) + [f"notion_id:{page_id}"],
    )


__all__ = ["ingest_notion_page"]
