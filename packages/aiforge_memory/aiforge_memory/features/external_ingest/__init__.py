"""External-source ingest (gap-9).

One thin entry point — :func:`ingest_external_source` — turns any
plain-text source (local file, http(s) URL, or stdin-style raw text)
into an AFM ``Doc_v2`` node plus a chunked set of ``Chunk_v2``-style
notes so the search bundle can rerank against it.

Specific connectors (Confluence, Slack, Jira, Notion …) live as thin
wrappers on top: each fetches its raw text via that system's API or
an MCP tool and then hands the text to this module. The bigger
ingestion surface ships as future PRs; what's here is the spine.
"""
from __future__ import annotations

from .ingest import ingest_external_source

__all__ = ["ingest_external_source"]
