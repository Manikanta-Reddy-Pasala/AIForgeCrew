"""Markdown renderer for parsed researcher briefs.

The Doer's prompt template includes the rendered brief verbatim, so
section headers (``**Relevant files**`` etc.) are stable contracts the
model has been instructed to look for. Keep that wording stable; if
you tweak it, sweep the Doer prompt at the same time.
"""
from __future__ import annotations


def _render_section(lines: list[str], title: str, items: list, fmt) -> None:
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
    """Render the parsed briefs into a Doer-friendly markdown block."""
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


__all__ = ["render_markdown"]
