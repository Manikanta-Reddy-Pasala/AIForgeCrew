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

# Markdown code-fence wrapper — local models often wrap JSON in ```json...```
# even when the prompt asks for raw JSON. Strip it before parsing.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Return the inside of a ```json ... ``` block, or ``text`` unchanged."""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text


def _find_balanced_array(text: str) -> str | None:
    """Return the first top-level ``[...]`` substring, or None.

    Used as a salvage path when the model emitted prose around a valid
    JSON array (e.g. "Sure, here you go: [...]. Let me know."). We bail
    on the first nesting mismatch — no recursive recovery — to keep the
    parser simple and predictable.
    """
    start = text.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _coerce_brief(entry: dict) -> dict:
    """Project a raw brief dict onto the canonical 5-field shape.

    Missing / wrong-typed fields default to empty rather than raising —
    a single broken entry shouldn't sink the whole brief list.
    """
    return {
        "subticket_id": str(entry.get("subticket_id") or "").strip(),
        "relevant_files": _list_of_dicts(entry.get("relevant_files")),
        "related_symbols": _list_of_dicts(entry.get("related_symbols")),
        "prior_facts": _list_of_str(entry.get("prior_facts")),
        "gotchas": _list_of_str(entry.get("gotchas")),
    }


def parse(raw: str) -> list[dict]:
    """Extract the brief list from ``raw`` model output.

    Returns ``[]`` when nothing parsable is found — caller decides
    whether to retry or proceed with a thin context. Parsing is layered:

    1. Strip a markdown ```json fence if present.
    2. Try ``json.loads`` on the remainder (the happy path).
    3. Salvage: find the first balanced ``[...]`` and parse that.

    Any non-list result, or a list with non-dict entries, is filtered.
    """
    if not raw:
        return []
    text = _strip_code_fence(raw.strip())

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _find_balanced_array(text)
        if salvaged is None:
            return []
        try:
            data = json.loads(salvaged)
        except json.JSONDecodeError:
            return []

    if not isinstance(data, list):
        return []
    return [_coerce_brief(e) for e in data if isinstance(e, dict)]


def _list_of_dicts(v: Any) -> list[dict]:
    return [x for x in (v or []) if isinstance(x, dict)]


def _list_of_str(v: Any) -> list[str]:
    return [str(x) for x in (v or []) if isinstance(x, (str, int, float))]


def _render_section(lines: list[str], title: str, items: list,
                    fmt) -> None:
    """Append a ``**title**`` block + bullet list when ``items`` non-empty.

    Centralises the per-section pattern so the four section types in a
    brief (relevant_files, related_symbols, prior_facts, gotchas) share
    one formatting rule. Mutates ``lines`` in place — caller owns it.
    """
    if not items:
        return
    lines.append("")
    lines.append(f"**{title}**")
    for item in items:
        lines.append(fmt(item))


def render_markdown(briefs: list[dict]) -> str:
    """Render the parsed briefs into a Doer-friendly markdown block.

    The Doer's prompt template includes the rendered brief verbatim, so
    section headers (``**Relevant files**`` etc.) are stable contracts
    the model has been instructed to look for.
    """
    if not briefs:
        # An empty brief is a valid outcome — model couldn't find anything
        # useful. Tell the Doer explicitly so it doesn't assume context
        # was injected silently.
        return "# Research Brief\n\n_(empty — Doer must explore on its own)_\n"

    lines = ["# Research Brief", ""]
    for b in briefs:
        sid = b.get("subticket_id") or "(unnamed)"
        lines.append(f"## Subticket: {sid}")
        _render_section(
            lines, "Relevant files", b["relevant_files"],
            lambda f: (f"- `{f.get('path', '?')}` — {f['why']}"
                      if f.get("why") else f"- `{f.get('path', '?')}`"),
        )
        _render_section(
            lines, "Related symbols", b["related_symbols"],
            lambda s: (f"- `{s.get('label', '?')}` @ "
                      f"`{s.get('source_file', '?')}` ({s['relation']})"
                      if s.get("relation")
                      else f"- `{s.get('label', '?')}` @ "
                           f"`{s.get('source_file', '?')}`"),
        )
        _render_section(lines, "Prior facts", b["prior_facts"],
                        lambda f: f"- {f}")
        _render_section(lines, "Gotchas", b["gotchas"],
                        lambda g: f"- ⚠ {g}")
        lines.append("")
    # Trim trailing blank from the last subticket section, then add one
    # final newline for clean POSIX file termination.
    return "\n".join(lines).rstrip() + "\n"


def persist(briefs: list[dict], out_dir: Path, ticket_id: str) -> Path:
    """Write the rendered brief to ``<out_dir>/<ticket_id>.md`` and return path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{ticket_id}.md"
    p.write_text(render_markdown(briefs))
    return p


__all__ = ["parse", "render_markdown", "persist"]
