"""OKR-envelope coverage across ALL knowledge/mapping/link md the system
writes (per user: memory, mapping, code links, service links — NOT code
chunks / code graph, which are mechanical). Every such md must parse cleanly
through the ONE standard (work_notes.parse_note) and carry frontmatter +
Objective.
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


# ── mapping + code links + service links: REPO_NOTES.md ──────────────────────

def _sample_notes():
    from aiforge_core.indexing.repo_notes import RepoNotes
    n = RepoNotes(repo="svc", worktree="/tmp/svc")
    n.purpose = "Order service."
    n.layout = ["  src/  (42 files)"]
    n.controllers = [{"file": "src/OrderController.java",
                      "class_path": "/orders",
                      "endpoints": ["GET /orders/{id}"]}]
    n.services = [{"name": "OrderService",
                   "interface": "src/OrderService.java",
                   "impl": "src/OrderServiceImpl.java"}]
    n.repositories = ["src/OrderRepo.java"]
    n.nats_subjects = {"publish": ["order.created"], "subscribe": []}
    n.kafka_topics = {"publish": [], "subscribe": ["inventory.updated"]}
    n.mongo_collections = ["orders"]
    n.http_clients = ["https://payments/v1/api/charge"]
    return n


def test_repo_notes_is_okr_envelope(cfg):
    from aiforge_core.indexing.repo_notes import render_markdown
    from aiforge_core.runtime import work_notes
    md = render_markdown(_sample_notes())
    parsed = work_notes.parse_note(md)
    assert parsed["frontmatter"]["kind"] == "repo"
    assert parsed["frontmatter"]["key"] == "svc"
    assert "updated_at" in parsed["frontmatter"]
    assert parsed["sections"]["objective"]          # Objective present
    # measurable Key Results = the scan counts
    krs = " ".join(parsed["sections"]["key_results"])
    assert "1 controllers" in krs and "1 services" in krs
    # code links + service links preserved in the body
    assert "OrderController.java" in parsed["body"]
    assert "order.created" in parsed["body"]         # NATS service link
    assert "inventory.updated" in parsed["body"]      # Kafka service link
    assert "payments/v1/api/charge" in parsed["body"] # outbound HTTP


def test_repo_notes_deterministic_when_updated_at_pinned(cfg):
    # two renders differ only by updated_at → identical once stripped
    from aiforge_core.indexing.repo_notes import render_markdown
    from aiforge_core.runtime import work_notes
    a = work_notes.parse_note(render_markdown(_sample_notes()))
    b = work_notes.parse_note(render_markdown(_sample_notes()))
    assert a["body"] == b["body"]
    assert a["sections"] == b["sections"]


# ── migration of existing legacy briefs ──────────────────────────────────────

def test_migrate_legacy_recent_brief(cfg):
    from aiforge_core.memory import md_store as m
    legacy = ("---\ntitle: svc memory (compacted)\nkind: compacted\n"
              "repo: svc\nsource: brief:svc\ncreated: 2026-07-01\n---\n\n"
              "# svc memory (compacted)\n\nConsolidated prose here.\n\n"
              "## Recent\n- fact alpha\n- fact beta\n")
    (m.brief_path("svc")).write_text(legacy, encoding="utf-8")
    r = m.migrate_to_okr()
    assert r["migrated"] == 1
    from aiforge_core.runtime import work_notes
    raw = (m.brief_path("svc")).read_text()
    parsed = work_notes.parse_note(raw)
    assert parsed["frontmatter"]["kind"] == "knowledge"
    assert parsed["sections"]["facts"] == ["fact alpha", "fact beta"]
    assert "Consolidated prose here." in parsed["body"]


def test_migrate_is_idempotent(cfg):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("svc", "already okr fact")     # writes OKR directly
    r1 = m.migrate_to_okr()
    assert r1["migrated"] == 0 and r1["skipped"] == 1
    r2 = m.migrate_to_okr()
    assert r2["migrated"] == 0


def test_migrate_only_touches_briefs(cfg):
    # a per-session note (not compacted-*) must be left alone
    from aiforge_core.memory import md_store as m
    m.write("some session", "body", kind="session", source="chat-x")
    m._brief_upsert("svc", "f")
    (m.brief_path("svc")).write_text(
        "---\nkind: compacted\n---\n# x\n## Recent\n- z\n", encoding="utf-8")
    r = m.migrate_to_okr()
    assert r["migrated"] == 1              # only the compacted brief
    files = [d["file"] for d in m.list_files()]
    assert any(f.startswith("compacted-svc") for f in files)


# ── re-compaction never double-nests the envelope ────────────────────────────

def test_recompact_no_double_envelope(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    m.capture("project_learning", "svc: fact one", repo="svc", topic="t")
    m.capture("project_learning", "svc: fact two", repo="svc", topic="t")
    m.compact(group_by="repo", min_group=2, summarize=False, archive_sources=False)
    m.capture("project_learning", "svc: fact three", repo="svc", topic="t")
    m.compact(group_by="repo", min_group=1, summarize=False, archive_sources=False)
    raw = (m.brief_path("svc")).read_text()
    # exactly ONE frontmatter block, ONE Objective — no nesting
    assert raw.count("## Objective") == 1
    assert raw.split("---", 2)[1].count("kind:") == 1
    parsed = work_notes.parse_note(raw)
    assert parsed["frontmatter"]["kind"] == "knowledge"
