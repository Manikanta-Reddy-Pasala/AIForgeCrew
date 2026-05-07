"""Tests for ``aiforge_core.runtime.research_brief``."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.runtime import research_brief as rb


def test_parse_clean_json_array():
    raw = (
        '[{"subticket_id":"S1","relevant_files":[{"path":"a.py","why":"main"}],'
        '"related_symbols":[],"prior_facts":["did similar last week"],'
        '"gotchas":[]}]'
    )
    out = rb.parse(raw)
    assert len(out) == 1
    assert out[0]["subticket_id"] == "S1"
    assert out[0]["relevant_files"][0]["path"] == "a.py"
    assert out[0]["prior_facts"] == ["did similar last week"]


def test_parse_strips_fence():
    raw = '```json\n[{"subticket_id":"S2","relevant_files":[]}]\n```'
    out = rb.parse(raw)
    assert len(out) == 1
    assert out[0]["subticket_id"] == "S2"


def test_parse_handles_surrounding_prose():
    raw = "Sure, here's the brief:\n[{\"subticket_id\":\"S3\"}]\nLet me know."
    out = rb.parse(raw)
    assert out and out[0]["subticket_id"] == "S3"


def test_parse_empty_string_returns_empty_list():
    assert rb.parse("") == []
    assert rb.parse("   ") == []


def test_parse_non_array_returns_empty():
    """A single object (not an array) is treated as malformed."""
    assert rb.parse('{"subticket_id":"S4"}') == []


def test_parse_drops_non_dict_entries():
    raw = '[{"subticket_id":"ok"}, "garbage", 42]'
    out = rb.parse(raw)
    assert len(out) == 1
    assert out[0]["subticket_id"] == "ok"


def test_render_empty_briefs():
    md = rb.render_markdown([])
    assert "Research Brief" in md
    assert "empty" in md.lower()


def test_render_full_brief_has_all_sections():
    briefs = [{
        "subticket_id": "S5",
        "relevant_files": [{"path": "x.py", "why": "core logic"}],
        "related_symbols": [{"label": "Foo", "source_file": "x.py", "relation": "calls"}],
        "prior_facts": ["fact A"],
        "gotchas": ["watch out"],
    }]
    md = rb.render_markdown(briefs)
    assert "Subticket: S5" in md
    assert "x.py" in md and "core logic" in md
    assert "Foo" in md and "calls" in md
    assert "fact A" in md
    assert "watch out" in md


def test_persist_writes_file(tmp_path: Path):
    briefs = [{
        "subticket_id": "S6",
        "relevant_files": [{"path": "a.py", "why": "y"}],
        "related_symbols": [], "prior_facts": [], "gotchas": [],
    }]
    out = rb.persist(briefs, tmp_path / "briefs", "ticket-42")
    assert out.exists()
    assert "S6" in out.read_text()
    assert out.name == "ticket-42.md"
