"""A dossier has ONE outline: note h1, envelope h2, each item h2, bodies below.

A linked item used to be an `## Linked jira` h2 immediately followed by the
item's own h1 — the hierarchy upside down in the middle of the preview.
"""
from __future__ import annotations

import re

from aiforge_core.runtime import context_gather as cg

PRIMARY = {"key": "ONE-7", "summary": "sync stalls", "status": "Open",
           "type": "Bug", "assignee": "ops", "url": "https://j/ONE-7",
           "description": "h1. Context\nIt stalls.\n# step one\n# step two",
           "comments": [{"author": "dev", "body": "line one\nline two"}]}
LINKED = {"id": "123", "title": "Runbook", "url": "https://c/123",
          "body": "<h1>Runbook</h1><p>Restart the <strong>pod</strong>.</p>"}


def _headings(md: str) -> list[tuple[int, str]]:
    out, fence = [], False
    for ln in md.split("\n"):
        if ln.startswith("```"):
            fence = not fence
        elif not fence and (m := re.match(r"^(#{1,6})\s+(.*)$", ln)):
            out.append((len(m.group(1)), m.group(2)))
    return out


def test_every_entity_is_an_h2_and_its_body_sits_below_it():
    md = cg._render_dossier("jira", "ONE-7", PRIMARY, [("confluence", LINKED)])
    heads = _headings(md)
    assert heads[0][0] == 1                         # the note title
    entity_heads = [t for lvl, t in heads if lvl == 2
                    and ("ONE-7" in t or "Linked" in t)]
    assert any("ONE-7" in t for t in entity_heads)
    assert any(t.startswith("Linked confluence: Runbook") for t in entity_heads)
    # no h1 anywhere after the title
    assert all(lvl >= 2 for lvl, _ in heads[1:]), heads


def test_the_numbered_list_is_not_rendered_as_headings():
    md = cg._render_dossier("jira", "ONE-7", PRIMARY, [])
    assert "1. step one" in md
    assert not any(t in ("step one", "step two") for _, t in _headings(md))


def test_markup_is_converted_in_the_dossier():
    md = cg._render_dossier("jira", "ONE-7", PRIMARY, [("confluence", LINKED)])
    assert "h1." not in md
    assert "<h1>" not in md
    assert "**pod**" in md


def test_a_multi_line_comment_stays_inside_its_quote():
    md = cg._md_for("jira", PRIMARY, level=2)
    assert "> **dev:** line one\n> line two" in md


def test_a_standalone_item_file_keeps_its_h1():
    """Written as its own .md, the item IS the document."""
    md = cg._md_for("confluence", LINKED)
    assert md.startswith("# Runbook")
