"""How a brief is cut down to fit the window, and which half survives.

A brief renders Key Results, Facts oldest-first, then the most recent
Learnings, then the consolidated body — so the old ``knowledge[:6000]`` head
slice kept the OLDEST facts, cut one mid-sentence, and dropped every Learning.
Split parts (``compacted-<key>-2.md`` …) were never read at all, so the biggest
briefs were the ones the model saw least of.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def _brief(md_store, key, facts, links=None, learnings=None):
    from aiforge_core.runtime import work_notes
    md_store.brief_path(key).write_text(
        work_notes.render_note("knowledge", key, title=key,
                               objective="Durable knowledge.", facts=facts,
                               learnings=learnings or [], links=links or [],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")


# ── the clip keeps whole lines, newest first ────────────────────────────────

def test_clip_keeps_whole_lines_and_the_newest_of_them():
    from aiforge_core.runtime import context_bundle as cb
    text = "\n".join(f"- fact {i:03d}" for i in range(100))
    out = cb._clip_knowledge(text, 200)
    assert len(out) <= 200
    assert "- fact 099" in out          # newest kept
    assert "- fact 000" not in out      # oldest dropped
    # every surviving line is whole
    assert all(ln.startswith(("- fact", "_…")) and len(ln.split()) == 3
               for ln in out.splitlines() if ln.startswith("- "))


def test_clip_says_that_older_entries_were_cut():
    from aiforge_core.runtime import context_bundle as cb
    out = cb._clip_knowledge("\n".join(f"- f{i}" for i in range(200)), 120)
    assert "trimmed" in out


def test_clip_leaves_text_that_fits_alone():
    from aiforge_core.runtime import context_bundle as cb
    assert cb._clip_knowledge("- one fact", 500) == "- one fact"


# ── split parts are read ────────────────────────────────────────────────────

def test_split_brief_parts_are_injected_too(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    _brief(md_store, "svc", ["part one fact"])
    _brief(md_store, "svc-2", ["part two fact"])
    _brief(md_store, "svc-3", ["part three fact"])
    out = context_bundle.project_brief_text("svc")
    assert "part one fact" in out
    assert "part two fact" in out
    assert "part three fact" in out


def test_part_scan_stops_at_the_first_gap(mem):
    """-2 missing means the brief is not split; -3 is then someone else's file
    (a slug that genuinely ends in a number), not a continuation."""
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    _brief(md_store, "svc", ["primary fact"])
    _brief(md_store, "svc-3", ["unrelated numbered brief"])
    out = context_bundle.project_brief_text("svc")
    assert "primary fact" in out
    assert "unrelated numbered brief" not in out


# ── the newest facts survive the budget ─────────────────────────────────────

def test_the_newest_facts_and_learnings_survive_a_full_brief(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    facts = [f"fact number {i:04d} " + "padding " * 12 for i in range(400)]
    _brief(md_store, "svc", facts, learnings=["the gotcha that matters most"])
    out = context_bundle.project_brief_text("svc")
    assert "the gotcha that matters most" in out   # Learnings render last
    assert "fact number 0399" in out               # newest fact
    assert "fact number 0000" not in out           # oldest dropped first


def test_the_global_brief_still_lands_under_a_huge_project_brief(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import context_bundle
    _brief(md_store, "svc", [f"svc fact {i} " + "x " * 40 for i in range(500)])
    _brief(md_store, "shared", ["never commit directly to main"])
    out = context_bundle.project_brief_text("svc")
    assert "never commit directly to main" in out
    assert len(out) <= context_bundle._BRIEF_TOTAL_CAP
