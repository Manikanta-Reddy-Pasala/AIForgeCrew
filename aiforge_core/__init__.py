"""Paperclip — AIForgeCrew orchestrator.

Phase P1 runtime. Reads `paperclip.config.yml` + `agents/<role>/permissions.yml`,
persists tickets + comments + audit in SQLite (`.paperclip/paperclip.db`).
All state transitions must match DESIGN.md §4 lifecycle.
"""
from __future__ import annotations

__version__ = "0.1.0"
