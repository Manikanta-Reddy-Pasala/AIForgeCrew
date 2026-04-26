"""Doer tool modules — one file per concern, KISS-style.

Each module exports either:
- ``SCHEMA``: OpenAI-compat function schema → injected into the
  Doer's tools_schema (when the tool is enabled).
- ``handle(...)``: pure logic the GA handler thin-wraps in
  ``do_<tool_name>``.

Plus shared helpers:
- ``llm_config.primary_cfg`` / ``fallback_cfg``: model session config.
- ``edit_verify``: post-patch git-diff display.
- ``read_tracker.ReadTracker``: per-run read cache + line numbering.
"""
from . import (
    aider_blocks, bash, batch, bulk_edit, conventions, edit_verify,
    glob, grep, java_refactor, lint, llm_config, read_tracker,
    readonly, repo_config, tests, tokens, undo, web_search,
)

__all__ = [
    "aider_blocks", "bash", "batch", "bulk_edit", "conventions",
    "edit_verify", "glob", "grep", "java_refactor", "lint",
    "llm_config", "read_tracker", "readonly", "repo_config",
    "tests", "tokens", "undo", "web_search",
]
