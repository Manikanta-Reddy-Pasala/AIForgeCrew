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
    p = m._resolve_md(name) or (m.memory_dir() / name)
    return p.read_text(encoding="utf-8")


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
    (m.brief_path("svc")).write_text(legacy, encoding="utf-8")
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
    p = m.brief_path("svc")
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


# ── structured (LLM) compaction into OKR sections + topic archiving ─────────

def _stub_consolidate_llm(monkeypatch):
    """Patch structured_complete so work_notes.consolidate takes its LLM path:
    the stub folds each new line of content into Facts (dedupe, keep existing)."""
    import json
    from types import SimpleNamespace as NS

    def fake(role, messages, response_model, **kw):
        payload = json.loads(next(m["content"] for m in messages
                                  if m["role"] == "user"))
        cur = payload["current_sections"]
        facts = list(cur.get("facts", []))
        for line in (payload["new_information"] or "").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and s not in facts and len(s) < 200:
                facts.append(s)
        return NS(objective=cur.get("objective", ""), key_results=[],
                  facts=facts, links=cur.get("links", []),
                  learnings=cur.get("learnings", []))
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", fake)


def test_topic_splits_into_linked_parts_when_oversize(cfg, monkeypatch):
    # a topic that outgrows the split cap becomes compacted-<topic>.md +
    # compacted-<topic>-2.md, each an OKR envelope, cross-referenced.
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    monkeypatch.setenv("AIFORGE_TOPIC_SPLIT_CAP", "500")   # tiny → forces a split
    _stub_consolidate_llm(monkeypatch)
    for i in range(12):
        m.capture("topic_learning", f"authfact number {i} " + "x" * 40,
                  repo=f"r{i}", topic="auth")
    r = m.compact(group_by="topic", min_group=1, summarize=True)
    files = {p.name for p in m.iter_briefs() if p.name.startswith("compacted-auth")}
    assert "compacted-auth.md" in files
    assert "compacted-auth-2.md" in files                 # split happened
    head = work_notes.parse_note(_raw("compacted-auth.md"))
    assert head["frontmatter"]["kind"] == "knowledge"     # OKR envelope
    assert "Continued in" in _raw("compacted-auth.md")    # forward cross-ref
    assert "main topic" in _raw("compacted-auth-2.md")    # back cross-ref


def test_topic_shrink_retires_stale_split_parts(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    monkeypatch.setenv("AIFORGE_TOPIC_SPLIT_CAP", "500")
    _stub_consolidate_llm(monkeypatch)
    for i in range(12):
        m.capture("topic_learning", f"bigfact {i} " + "y" * 40, repo=f"r{i}", topic="ops")
    m.compact(group_by="topic", min_group=1, summarize=True)
    assert (m.brief_path("ops-2")).exists()
    p2_facts = work_notes.parse_note(_raw("compacted-ops-2.md"))["sections"]["facts"]
    assert p2_facts                                   # part 2 holds real facts
    # recompact with a HUGE cap + one new unit → the topic re-folds (reading ALL
    # parts), everything fits in one file, and the stale part-2 is retired.
    monkeypatch.setenv("AIFORGE_TOPIC_SPLIT_CAP", "100000")
    m.capture("topic_learning", "one more ops fact", repo="rx", topic="ops")
    m.compact(group_by="topic", min_group=1, summarize=True)
    assert not (m.brief_path("ops-2")).exists()
    head = _raw("compacted-ops.md")
    # facts from the retired part 2 were re-folded into the single file (not lost)
    assert p2_facts[0] in head


def test_compact_structured_writes_okr_facts_when_model_available(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    _stub_consolidate_llm(monkeypatch)
    m.capture("project_learning", "svc: validate at boundary", repo="svc", topic="a")
    m.capture("project_learning", "svc: no direct mongo", repo="svc", topic="b")
    r = m.compact(group_by="repo", min_group=2, summarize=True)
    assert r["groups"].get("svc") == 2
    parsed = work_notes.parse_note(_raw("compacted-svc.md"))
    assert parsed["frontmatter"]["kind"] == "knowledge"
    facts = parsed["sections"]["facts"]
    # content landed in STRUCTURED Facts, not a prose body blob
    assert any("validate at boundary" in f for f in facts)
    assert any("no direct mongo" in f for f in facts)


def test_compact_brief_carries_tags(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    _stub_consolidate_llm(monkeypatch)
    m.capture("project_learning", "svc: rule a", repo="svc", topic="arch")
    m.capture("project_learning", "svc: rule b", repo="svc", topic="perf")
    m.compact(group_by="repo", min_group=2, summarize=True)
    fm = work_notes.parse_note(_raw("compacted-svc.md"))["frontmatter"]
    # the units' repo:/topic: tags land in the brief frontmatter (colon kept)
    assert "repo:svc" in fm["tags"]
    assert any(t.startswith("topic:") for t in fm["tags"])


def test_topic_compact_archives_raw_units(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    _stub_consolidate_llm(monkeypatch)
    m.capture("topic_learning", "auth: rotate keys 90d", repo="a", topic="auth")
    m.capture("topic_learning", "auth: mTLS between svcs", repo="b", topic="auth")
    r = m.compact(group_by="topic", min_group=2, summarize=True,
                  archive_sources=True)
    assert r["files_in"] == 2                         # 2 raw units MOVED out
    live = {p.name for p in m._all_md_files()}
    assert any(n.startswith("compacted-") for n in live)   # topic brief stays
    archived = list((m.memory_dir() / "archive").rglob("*.md"))
    assert len(archived) == 2                         # raw session notes cleared


def test_cleanup_folds_cryptic_into_topic(cfg, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    _stub_consolidate_llm(monkeypatch)
    # cryptic id-keyed / per-kind briefs (the sprawl) + one real topic brief
    for stem, fact in [("compacted-1852458641", "conf: page id fact"),
                       ("compacted-clr-3049", "ticket clr fact"),
                       ("compacted-session-1", "session one fact"),
                       ("compacted-auth", "real auth topic fact"),
                       ("compacted-auth-2", "auth split overflow fact")]:
        (m.memory_dir() / f"{stem}.md").write_text(
            work_notes.render_note("knowledge", stem[len("compacted-"):],
                                   title=stem, facts=[fact]), encoding="utf-8")
    plan = m.cleanup_legacy_compacted(dry_run=True)
    assert set(plan["stale"]) == {"compacted-1852458641.md",
                                  "compacted-clr-3049.md", "compacted-session-1.md"}
    assert "compacted-auth.md" not in plan["stale"]         # real topic kept
    assert "compacted-auth-2.md" not in plan["stale"]       # split part kept
    r = m.cleanup_legacy_compacted()
    assert r["ok"] and r["folded"] == 3 and r["facts"] >= 3
    live = {p.name for p in m._all_md_files()}
    assert "compacted-1852458641.md" not in live            # cryptic gone
    assert "compacted-clr-3049.md" not in live
    archived = {p.name for p in (m.memory_dir() / "archive").rglob("*.md")}
    assert "compacted-1852458641.md" in archived            # reversible
