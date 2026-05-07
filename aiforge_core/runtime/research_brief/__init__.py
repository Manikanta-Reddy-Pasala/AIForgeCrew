r"""Helpers for parsing + rendering + persisting Researcher output.

The Researcher emits a JSON array (see
:mod:`aiforge_core.runtime.prompts_extended.researcher`). This package
splits the three concerns into focused modules:

    parser.py    — :func:`parse` — raw model text → list[dict]
    renderer.py  — :func:`render_markdown` — list[dict] → str (markdown)
    persist.py   — :func:`persist` — list[dict] → file on disk

Public re-exports preserve the old import surface
(``research_brief.parse``, ``.render_markdown``, ``.persist``).
"""
from __future__ import annotations

from .parser import parse
from .renderer import render_markdown
from .persist import persist

__all__ = ["parse", "render_markdown", "persist"]
