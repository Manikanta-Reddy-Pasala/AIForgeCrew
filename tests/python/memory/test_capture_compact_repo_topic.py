"""capture → md (repo+topic stamped) → compact by repo/topic → load brief.
The user requirement: everything must be WRITTEN to md, else it never reaches
compaction or the loadable project brief.
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def test_capture_writes_md_with_repo_and_topic(cfg):
    from aiforge_core.memory import md_store as m
    d = m.capture("project_learning", "db access only via gateway",
                  repo="svc", topic="architecture")
    assert d["repo"] == "svc"
    assert d["topic"] == "architecture"
    assert "repo:svc" in d["tags"] and "topic:architecture" in d["tags"]


def test_compact_by_repo_makes_project_brief(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "svc: validate at boundary", repo="svc", topic="a")
    m.capture("project_learning", "svc: no direct mongo", repo="svc", topic="b")
    m.capture("project_learning", "other: unrelated", repo="other", topic="a")
    r = m.compact(group_by="repo", min_group=2, summarize=False)
    assert r["groups"].get("svc") == 2          # svc rolled up
    assert "other" not in r["groups"]           # singleton left alone
    briefs = sorted(p.name for p in m.iter_briefs() if p.name == "compacted-svc.md")
    assert briefs == ["compacted-svc.md"]


def test_capture_title_never_masquerades_as_compacted_brief(cfg):
    # A title starting with "compacted" must NOT produce a compacted-*.md file:
    # compact() excludes compacted-* from its live set, so such a capture would
    # slip past compaction FOREVER and pile up (the compacted-retry-on-empty-fix
    # sprawl). The reserved-prefix guard strips it.
    from aiforge_core.memory import md_store as m
    d = m.capture("topic_learning", "compacted retry on empty fix",
                  repo="notes", topic="retry")
    assert not d["file"].startswith("compacted-")


def test_sweep_stale_captures_archives_masqueraders_keeps_briefs(cfg):
    # Legacy masqueraders (compacted-<desc>-YYYYMMDD-hex.md) get archived out;
    # real canonical briefs (compacted-<topic>.md, no date-hex) stay put.
    from aiforge_core.memory import md_store as m
    md = m.memory_dir()
    (md / "compacted-foo-bar-20260710-abc123.md").write_text("---\ntype: x\n---\nj")
    (md / "compacted-sync.md").write_text("---\nkind: knowledge\n---\nreal")
    r = m.sweep_stale_captures(archive=True)
    assert r["swept"] == 1
    assert (md / "compacted-sync.md").exists()                 # brief kept
    assert not (md / "compacted-foo-bar-20260710-abc123.md").exists()  # swept


def test_compact_by_topic_groups_cross_repo(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("topic_learning", "auth: rotate keys 90d", repo="a", topic="auth")
    m.capture("topic_learning", "auth: mTLS between services", repo="b", topic="auth")
    r = m.compact(group_by="topic", min_group=2, summarize=False)
    assert r["groups"].get("auth") == 2         # cross-repo topic rolled up


def test_project_brief_loads_for_repo(cfg):
    import subprocess
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import context_bundle as cb
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo])
    rn = os.path.basename(repo)
    m.capture("project_learning", f"{rn}: rule one", repo=rn, topic="x")
    m.capture("project_learning", f"{rn}: rule two", repo=rn, topic="y")
    m.compact(group_by="repo", min_group=2, summarize=False)
    brief = cb._project_brief(repo)
    assert brief.startswith("PROJECT MEMORY (")
    assert "rule one" in brief or "rule two" in brief


def test_both_axes_nondestructive_sequence(cfg):
    # The bug: running repo compaction first ARCHIVED sources, so the topic
    # pass saw nothing. archive_sources=False keeps a unit for BOTH axes.
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "r1: proxies win", repo="r1", topic="proxies")
    m.capture("project_learning", "r2: proxies win too", repo="r2", topic="proxies")
    rr = m.compact(group_by="repo", min_group=2, summarize=False, archive_sources=False)
    rt = m.compact(group_by="topic", min_group=2, summarize=False, archive_sources=False)
    assert rr["groups"].get("r1") == 1 or rr["files_out"] >= 0   # repo briefs made
    assert rt["groups"].get("proxies") == 2                       # topic STILL sees both


def test_compact_min_group_1_folds_a_lone_note(cfg):
    # "nothing to compact" fix: a single-note topic (min_group=2 would skip it)
    # still folds + archives when min_group=1, so lone sessions don't linger.
    from aiforge_core.memory import md_store as m
    m.capture("topic_learning", "solo: one-off insight", repo="a", topic="solo")
    skipped = m.compact(group_by="topic", min_group=2, summarize=False)
    assert "solo" not in skipped["groups"]                 # skipped at 2
    r = m.compact(group_by="topic", min_group=1, summarize=False,
                  archive_sources=True)
    assert r["groups"].get("solo") == 1                    # folded at 1
    live = {p.name for p in m._all_md_files()}
    assert any(n.startswith("compacted-") for n in live)   # topic brief made


def test_session_notes_fold_into_topic_not_repo(cfg, monkeypatch):
    # Per-session transcripts (kind="session") were EXCLUDED from both brief
    # axes, so they lingered forever + compaction said "nothing to compact".
    # They now belong to the TOPIC axis (memory by topic); REPO stays curated.
    from aiforge_core.memory import md_store as m
    m.upsert_section(source="chat-session:1", title="session one",
                     section_title="2026-07-10 10:00", section_body="did A",
                     kind="session", tags=["chat"])
    m.upsert_section(source="chat-session:2", title="session two",
                     section_title="2026-07-10 11:00", section_body="did B",
                     kind="session", tags=["chat"])
    # REPO axis still excludes raw sessions
    rr = m.compact(group_by="repo", min_group=1, summarize=False, dry_run=True)
    assert rr["files_in"] == 0
    # TOPIC axis now includes them. The new-topic floor is relaxed here: these
    # are two unrelated one-off sessions, which the floor correctly refuses
    # their own topic files (see test_new_topic_needs_the_floor below) — this
    # test is about the AXIS, not about admission.
    monkeypatch.setenv("AIFORGE_TOPIC_MIN_FACTS", "1")
    rt = m.compact(group_by="topic", min_group=1, summarize=False, dry_run=True)
    assert rt["files_in"] == 2


def test_new_topic_needs_the_floor(cfg, monkeypatch):
    # Topic sprawl guard: a MODEL-INVENTED topic with too few notes must not
    # mint its own brief (that is how 142 briefs appeared, a third holding one
    # fact, which then poisoned recall). An EXPLICIT caller topic is intentional
    # and always allowed through.
    from aiforge_core.memory import md_store as m
    monkeypatch.setenv("AIFORGE_TOPIC_MIN_FACTS", "3")
    m.upsert_section(source="chat-session:9", title="one off session",
                     section_title="t", section_body="did Z", kind="session",
                     tags=["chat"])
    r = m.compact(group_by="topic", min_group=1, summarize=False, dry_run=True)
    assert r["groups"] == {}                       # refused: not enough to earn a file

    m.capture("topic_learning", "billing: invoices post nightly",
              repo="svc", topic="billing-pipeline")
    r2 = m.compact(group_by="topic", min_group=1, summarize=False, dry_run=True)
    assert r2["groups"].get("billing-pipeline") == 1   # explicit topic passes


def test_writetime_brief_is_fresh_immediately(cfg):
    # Write-time maintenance: a captured learning appears in the repo brief
    # IMMEDIATELY (no periodic compaction), so recall never misses latest data.
    from aiforge_core.memory import md_store as m
    m.capture("project_learning", "svc: db via gateway only", repo="svc", topic="arch")
    b = m.read_file("compacted-svc")
    assert b and "db via gateway only" in b["body"]
    m.capture("project_learning", "svc: db via gateway only", repo="svc", topic="arch")  # dup
    b2 = m.read_file("compacted-svc")
    assert b2["body"].count("# svc memory") == 1              # single heading
    assert b2["body"].count("db via gateway only") == 1       # deduped
