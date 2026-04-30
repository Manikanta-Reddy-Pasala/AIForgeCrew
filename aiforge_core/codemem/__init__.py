"""codemem — unified code memory for AIForgeCrew.

Single read API for code context (Repo / Service / File / Symbol +
Chunk vectors). Replaces the legacy index/ + memory/code_context.py
stack incrementally. See docs/superpowers/specs/2026-04-30-unified-code-memory-design.md.
"""
from __future__ import annotations

SCHEMA_VERSION = "codemem-v1"
