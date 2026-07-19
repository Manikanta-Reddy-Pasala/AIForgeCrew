"""Peer-to-peer replication of the markdown memory tree.

Markdown is the source of truth and ``memory.db`` is a local derived index, so
syncing means replicating text files. See
``docs/superpowers/specs/2026-07-19-p2p-shared-memory-design.md``.
"""
from __future__ import annotations

__all__ = ["manifest", "merge"]
