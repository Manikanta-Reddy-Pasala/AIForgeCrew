r"""Helpers for parsing + persisting Researcher output.

The Researcher emits a JSON array (see ``prompts_extended.RESEARCHER``).
This module:

* Validates the shape (best-effort — accepts partial briefs so a single
  malformed subticket entry doesn't sink the run)
* Renders a flat ``research_brief.md`` per ticket so the Doer's prompt
  can include it verbatim
* Tolerates the model wrapping JSON in markdown ``json`` code fences
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def parse(raw: str) -> list[dict]:
    """Extract the brief list from ``raw`` model output.

    Returns ``[]`` when nothing parsable is found — caller decides
    whether to retry or proceed with a thin context.
    """
    if not raw:
        return []
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try the first balanced [ ... ] in the string.
        start = text.find("[")
        if start < 0:
            return []
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i+1])
                        break
                    except json.JSONDecodeError:
                        return []
        else:
            return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        out.append({
            "subticket_id": str(entry.get("subticket_id") or "").strip(),
            "relevant_files": _list_of_dicts(entry.get("relevant_files")),
            "related_symbols": _list_of_dicts(entry.get("related_symbols")),
            "prior_facts": _list_of_str(entry.get("prior_facts")),
            "gotchas": _list_of_str(entry.get("gotchas")),
        })
    return out


def _list_of_dicts(v: Any) -> list[dict]:
    return [x for x in (v or []) if isinstance(x, dict)]


def _list_of_str(v: Any) -> list[str]:
    return [str(x) for x in (v or []) if isinstance(x, (str, int, float))]


def render_markdown(briefs: list[dict]) -> str:
    """Render the parsed briefs into a Doer-friendly markdown block."""
    if not briefs:
        return "# Research Brief\n\n_(empty — Doer must explore on its own)_\n"
    lines = ["# Research Brief", ""]
    for b in briefs:
        sid = b.get("subticket_id") or "(unnamed)"
        lines.append(f"## Subticket: {sid}")
        if b["relevant_files"]:
            lines.append("**Relevant files**")
            for f in b["relevant_files"]:
                p = f.get("path", "?")
                why = f.get("why", "")
                lines.append(f"- `{p}` — {why}" if why else f"- `{p}`")
        if b["related_symbols"]:
            lines.append("")
            lines.append("**Related symbols**")
            for s in b["related_symbols"]:
                lab = s.get("label", "?")
                sf = s.get("source_file", "?")
                rel = s.get("relation", "")
                lines.append(f"- `{lab}` @ `{sf}` ({rel})" if rel
                             else f"- `{lab}` @ `{sf}`")
        if b["prior_facts"]:
            lines.append("")
            lines.append("**Prior facts**")
            for f in b["prior_facts"]:
                lines.append(f"- {f}")
        if b["gotchas"]:
            lines.append("")
            lines.append("**Gotchas**")
            for g in b["gotchas"]:
                lines.append(f"- ⚠ {g}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def persist(briefs: list[dict], out_dir: Path, ticket_id: str) -> Path:
    """Write the rendered brief to ``<out_dir>/<ticket_id>.md`` and return path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ticket_id}.md"
    p.write_text(render_markdown(briefs))
    return p


__all__ = ["parse", "render_markdown", "persist"]
