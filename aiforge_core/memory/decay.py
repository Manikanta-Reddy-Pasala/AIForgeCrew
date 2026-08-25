"""Memory decay / expiry.

This archived stale rows in the Postgres ``memories`` table, which has been
removed (SQLite-only build). The embedded SQLite store manages its own
dedupe/decay (see the ``memory-dedup`` daily task), so ``run()`` is now a
soft no-op kept as a stable ``aiforge memory decay`` entry point.

Public surface:
- ``run() -> dict``
"""
from __future__ import annotations


def run() -> dict:
    """No-op — the Postgres memories table was removed (SQLite-only build)."""
    return {"skipped": "postgres backend removed (SQLite-only build)"}
