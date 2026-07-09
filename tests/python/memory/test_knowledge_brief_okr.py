"""Knowledge briefs (compacted-<scope>.md) use the SAME Google-OKR envelope
as the managed work notes: frontmatter + Objective + Facts (write-time inbox,
deduped) + Learnings + free consolidated body. Legacy '## Recent' briefs
migrate in place on first touch.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def _raw(name):
    from aiforge_core.memory import md_store as m
    return (m.memory_dir() / name).read_text(encoding="utf-8")


def test_new_brief_is_okr_envelope(cfg):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "db access only via gateway", topic="arch")
    raw = _raw("compacted-svc.md")
    assert raw.startswith("---\n")
    assert 'kind: "knowledge"' in raw
    assert 'key: "svc"' in raw
    assert "## Objective" in raw
    assert "## Facts" in raw
    assert "- [arch] db access only via gateway" in raw


def test_facts_dedupe_across_topics(cfg):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "rotate keys 90d", topic="auth")
    m._brief_upsert("svc", "rotate keys 90d", topic="ops")   # same fact
    m._brief_upsert("svc", "rotate keys 90d")                # no topic
    assert _raw("compacted-svc.md").count("rotate keys 90d") == 1


def test_okr_parse_roundtrip_via_work_notes(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    m._brief_upsert("svc", "fact one")
    m._brief_upsert("svc", "fact two")
    parsed = work_notes.parse_note(_raw("compacted-svc.md"))
    assert parsed["frontmatter"]["kind"] == "knowledge"
    assert parsed["sections"]["facts"] == ["fact one", "fact two"]


def test_legacy_recent_brief_migrates(cfg):
    from aiforge_core.memory import md_store as m
    legacy = ("---\ntitle: svc memory (compacted)\nkind: compacted\n"
              "repo: svc\nsource: brief:svc\n---\n\n"
              "# svc memory (compacted)\n\nOld consolidated prose.\n\n"
              "## Recent\n- old bullet one\n- old bullet two\n")
    (m.memory_dir() / "compacted-svc.md").write_text(legacy, encoding="utf-8")
    m._brief_upsert("svc", "new fact")
    raw = _raw("compacted-svc.md")
    assert 'kind: "knowledge"' in raw            # migrated envelope
    assert "## Recent" not in raw                # legacy tail gone
    assert "- old bullet one" in raw             # ...migrated into Facts
    assert "- new fact" in raw
    assert "Old consolidated prose." in raw      # prose body preserved


def test_compact_repo_axis_writes_okr_brief(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "svc: validate at boundary", repo="svc", topic="a")
    m.capture("project_learning", "svc: no direct mongo", repo="svc", topic="b")
    r = m.compact(group_by="repo", min_group=2, summarize=False)
    assert r["groups"].get("svc") == 2
    raw = _raw("compacted-svc.md")
    assert 'kind: "knowledge"' in raw
    assert "## Objective" in raw
    assert "validate at boundary" in raw and "no direct mongo" in raw


def test_compact_preserves_learnings(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    m.capture("project_learning", "svc: rule one", repo="svc", topic="x")
    m.capture("project_learning", "svc: rule two", repo="svc", topic="y")
    m.compact(group_by="repo", min_group=2, summarize=False)
    # hand-add a learning (as the curator would)
    p = m.memory_dir() / "compacted-svc.md"
    work_notes.update_note(str(p), learnings=["2026-07-10: svc split into two"])
    m.capture("project_learning", "svc: rule three", repo="svc", topic="z")
    m.compact(group_by="repo", min_group=1, summarize=False)
    raw = _raw("compacted-svc.md")
    assert "2026-07-10: svc split into two" in raw   # learning survives compact


def test_project_brief_reader_still_works(cfg):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("shared", "global lesson: pin deps")
    b = m.read_file("compacted-shared")
    assert b and "global lesson: pin deps" in b["body"]
    assert b["kind"] == "knowledge"                  # quotes stripped


def test_writetime_then_compact_then_writetime(cfg):
    # full cycle: inbox facts -> compact folds them into body -> new facts
    # accumulate again in a clean Facts section
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "svc: alpha", repo="svc", topic="t")
    m.capture("project_learning", "svc: beta", repo="svc", topic="t")
    m.compact(group_by="repo", min_group=2, summarize=False)
    m._brief_upsert("svc", "gamma fresh fact")
    raw = _raw("compacted-svc.md")
    assert "- gamma fresh fact" in raw
    assert "alpha" in raw and "beta" in raw          # folded content kept
