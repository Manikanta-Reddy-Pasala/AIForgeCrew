"""External-source connectors built on the gap-9 ingest spine.

Each connector:
1. Fetches raw text from the source (Linear / Jira / Slack / Notion).
2. Calls :func:`ingest_external_source` with sensible defaults.

KISS: minimal HTTP, no SDK. Auth via env vars only. Soft-fail.
"""
from __future__ import annotations

from .confluence import ingest_confluence_page
from .jira import ingest_jira_issue
from .linear import ingest_linear_issue
from .notion import ingest_notion_page
from .slack import ingest_slack_channel

__all__ = [
    "ingest_confluence_page",
    "ingest_jira_issue",
    "ingest_linear_issue",
    "ingest_notion_page",
    "ingest_slack_channel",
]
