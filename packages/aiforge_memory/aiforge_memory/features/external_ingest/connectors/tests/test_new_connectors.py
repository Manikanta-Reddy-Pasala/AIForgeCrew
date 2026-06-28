"""Confluence / Slack / Notion connector unit tests (gap M5).

Each connector mirrors the jira/linear contract: a monkeypatchable
``_fetch`` reads creds from env + does the stdlib-urllib HTTP call, a
``_render`` normalizes the raw API payload to markdown text, and the
``ingest_*`` wrapper hands that text to the ingest spine with the right
``source_type``. The HTTP layer is monkeypatched so no network is hit.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aiforge_memory.features.external_ingest.connectors import (
    confluence, notion, slack,
)


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

_CONFLUENCE_PAGE = {
    "id": "12345",
    "title": "Runbook: Restart NATS",
    "body": {
        "storage": {
            "value": "<h1>Restart NATS</h1><p>Step one.</p><p>Step two.</p>",
        }
    },
}


def test_confluence_render_strips_html() -> None:
    text = confluence._render(_CONFLUENCE_PAGE)
    assert "Runbook: Restart NATS" in text
    assert "Step one." in text
    assert "Step two." in text
    assert "<p>" not in text and "<h1>" not in text


def test_confluence_ingest_passes_source_type(monkeypatch) -> None:
    monkeypatch.setattr(confluence, "_fetch", lambda page_id: _CONFLUENCE_PAGE)
    captured: dict = {}

    def fake_ingest(driver, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "doc_id": "d1", "note_ids": ["n1"], "errors": []}

    monkeypatch.setattr(confluence, "ingest_external_source", fake_ingest)
    driver = MagicMock()
    out = confluence.ingest_confluence_page(driver, page_id="12345", repo="r1")

    assert out["ok"] is True
    assert captured["source_type"] == "confluence"
    assert captured["repo"] == "r1"
    assert "Step one." in captured["source"]
    assert "confluence_id:12345" in captured["tags"]


def test_confluence_missing_creds_soft_fails(monkeypatch) -> None:
    monkeypatch.setattr(confluence, "_fetch", lambda page_id: None)
    out = confluence.ingest_confluence_page(MagicMock(), page_id="x", repo="r")
    assert out["ok"] is False
    assert "error" in out


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

_SLACK_HISTORY = {
    "ok": True,
    "messages": [
        {"user": "U1", "ts": "1700000000.0001", "text": "deploy is green"},
        {"user": "U2", "ts": "1700000100.0002", "text": "thanks, merging"},
    ],
}


def test_slack_render_joins_messages() -> None:
    text = slack._render("C123", _SLACK_HISTORY)
    assert "deploy is green" in text
    assert "thanks, merging" in text
    assert "U1" in text


def test_slack_ingest_passes_source_type(monkeypatch) -> None:
    monkeypatch.setattr(slack, "_fetch", lambda channel: _SLACK_HISTORY)
    captured: dict = {}

    def fake_ingest(driver, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "doc_id": "d1", "note_ids": ["n1", "n2"],
                "errors": []}

    monkeypatch.setattr(slack, "ingest_external_source", fake_ingest)
    out = slack.ingest_slack_channel(MagicMock(), channel="C123", repo="r1")

    assert out["ok"] is True
    assert captured["source_type"] == "slack"
    assert "deploy is green" in captured["source"]
    assert "slack_channel:C123" in captured["tags"]


def test_slack_missing_token_soft_fails(monkeypatch) -> None:
    monkeypatch.setattr(slack, "_fetch", lambda channel: None)
    out = slack.ingest_slack_channel(MagicMock(), channel="C123", repo="r")
    assert out["ok"] is False
    assert "error" in out


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

_NOTION_BLOCKS = {
    "results": [
        {
            "type": "heading_1",
            "heading_1": {"rich_text": [{"plain_text": "Design Doc"}]},
        },
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "First line."},
                                        {"plain_text": " more."}]},
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": "a point"}]},
        },
    ]
}


def test_notion_render_flattens_blocks() -> None:
    text = notion._render("page-1", _NOTION_BLOCKS)
    assert "Design Doc" in text
    assert "First line. more." in text
    assert "a point" in text


def test_notion_ingest_passes_source_type(monkeypatch) -> None:
    monkeypatch.setattr(notion, "_fetch", lambda page_id: _NOTION_BLOCKS)
    captured: dict = {}

    def fake_ingest(driver, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "doc_id": "d1", "note_ids": ["n1"], "errors": []}

    monkeypatch.setattr(notion, "ingest_external_source", fake_ingest)
    out = notion.ingest_notion_page(MagicMock(), page_id="page-1", repo="r1")

    assert out["ok"] is True
    assert captured["source_type"] == "notion"
    assert "First line. more." in captured["source"]
    assert "notion_id:page-1" in captured["tags"]


def test_notion_missing_token_soft_fails(monkeypatch) -> None:
    monkeypatch.setattr(notion, "_fetch", lambda page_id: None)
    out = notion.ingest_notion_page(MagicMock(), page_id="p", repo="r")
    assert out["ok"] is False
    assert "error" in out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_connectors_registered() -> None:
    from aiforge_memory.features.external_ingest import connectors as pkg
    assert hasattr(pkg, "ingest_confluence_page")
    assert hasattr(pkg, "ingest_slack_channel")
    assert hasattr(pkg, "ingest_notion_page")
