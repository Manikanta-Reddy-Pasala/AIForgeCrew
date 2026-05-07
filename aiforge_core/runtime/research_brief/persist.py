"""Persistence for rendered research briefs.

Single concern: write the markdown to disk. The orchestrator passes
in ``out_dir`` so this module owns no path policy of its own — it
just creates the dir if missing and returns the final path.
"""
from __future__ import annotations

from pathlib import Path

from .renderer import render_markdown


def persist(briefs: list[dict], out_dir: Path, ticket_id: str) -> Path:
    """Write the rendered brief to ``<out_dir>/<ticket_id>.md`` and return path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ticket_id}.md"
    p.write_text(render_markdown(briefs))
    return p


__all__ = ["persist"]
