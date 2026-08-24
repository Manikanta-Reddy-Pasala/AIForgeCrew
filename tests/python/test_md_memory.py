"""Markdown-file memory store: write/list/read/ingest + frontmatter."""
import json as _json  # noqa: F401

import pytest


@pytest.fixture
def md(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib

    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    return md_store


def test_write_creates_file_with_frontmatter(md):
    d = md.write("My Title", "the body text", kind="session", tags=["a", "b"])
    assert d["title"] == "My Title"
    assert d["kind"] == "session"
    # per-run captures live in the captures/ subfolder, not the memory root —
    # ask the store where instead of hardcoding the layout.
    path = md.captures_dir() / d["file"]
    raw = path.read_text()
    assert raw.startswith("---")
    assert "title: My Title" in raw
    assert "the body text" in raw


def test_list_and_read(md):
    md.write("Note one", "body one")
    md.write("Note two", "body two")
    names = {f["title"] for f in md.list_files()}
    assert {"Note one", "Note two"} <= names
    first = md.list_files()[0]
    full = md.read_file(first["name"])
    assert full["body"]


def test_ingest_dir_picks_up_handwritten(md):
    # drop a raw md file by hand
    (md.memory_dir() / "manual.md").write_text(
        "---\ntitle: Hand note\nkind: gotcha\n---\n\nremember this\n")
    out = md.ingest_dir()
    assert out["ok"]
    assert out["ingested"] >= 1
    assert any(f["title"] == "Hand note" for f in md.list_files())


def test_read_missing_returns_none(md):
    assert md.read_file("nope") is None


def test_delete(md):
    d = md.write("ToDelete", "x")
    assert md.delete_file(d["name"]) is True
    assert md.read_file(d["name"]) is None


def test_upsert_section_one_file_per_source(md):
    a = md.upsert_section(source="chat-session:7", title="My Session",
                          section_title="t1", section_body="first")
    b = md.upsert_section(source="chat-session:7", title="My Session",
                          section_title="t2", section_body="second")
    assert a["file"] == b["file"]            # same file, updated
    assert a["file"] == "my-session.md"      # full readable name, no hash
    files = list(md.captures_dir().glob("*.md"))
    assert len(files) == 1
    raw = files[0].read_text()
    assert "## t1" in raw
    assert "## t2" in raw
    assert "first" in raw
    assert "second" in raw


def test_upsert_distinct_sources_distinct_files(md):
    md.upsert_section(source="chat-session:1", title="Same Name",
                      section_title="x", section_body="a")
    md.upsert_section(source="chat-session:2", title="Same Name",
                      section_title="y", section_body="b")
    assert len(list(md.captures_dir().glob("*.md"))) == 2


def test_compact_groups_by_kind(md):
    for i in range(3):
        md.write(f"Session {i}", f"# Hdr\n\nbody {i}", kind="session", tags=["chat"])
    md.write("Note A", "a note", kind="note")
    md.write("Note B", "b note", kind="note")
    md.write("Lonely", "single", kind="rule")
    plan = md.compact(group_by="kind", dry_run=True, summarize=False)
    assert plan["groups"] == {"note": 2, "session": 3}
    assert plan["files_out"] == 2
    r = md.compact(group_by="kind", summarize=False)
    after = {f["file"] for f in md.list_files()}
    assert "compacted-session.md" in after
    assert "compacted-note.md" in after
    assert not any(f.startswith("session-") for f in after)   # originals archived
    assert any("lonely" in f for f in after)                  # singleton kept
    body = md.read_file("compacted-session.md")["body"]
    assert body.count("## Session") == 3
    assert "### Hdr" in body


def test_compact_idempotent_singletons(md):
    md.write("One", "x", kind="note")
    assert md.compact(group_by="kind", summarize=False)["files_out"] == 0     # nothing to merge


def test_compact_dry_run_writes_nothing(md):
    for i in range(2):
        md.write(f"S{i}", "x", kind="session")
    before = {f["file"] for f in md.list_files()}
    md.compact(group_by="kind", dry_run=True, summarize=False)
    assert {f["file"] for f in md.list_files()} == before


def test_compact_summarize_uses_llm(md, monkeypatch):
    # Mock the LLM consolidation so the test is hermetic + deterministic.
    import aiforge_core.memory.md_store as ms
    calls = {"n": 0}

    def fake_summarize(blocks, role):
        calls["n"] += 1
        return "## Consolidated\n\n- merged " + str(len(blocks)) + " blocks"

    monkeypatch.setattr(ms._compact, "_summarize_notes", fake_summarize)
    for i in range(3):
        md.write(f"S{i}", f"body {i}", kind="session")
    r = md.compact(group_by="kind", summarize=True)
    assert calls["n"] == 1                                   # one group → one call
    assert r["summarized"] == ["compacted-session.md"]
    body = md.read_file("compacted-session.md")["body"]
    assert "## Consolidated" in body
    assert "## S0" not in body


def test_compact_summarize_falls_back_to_merge(md, monkeypatch):
    import aiforge_core.memory.md_store as ms
    monkeypatch.setattr(ms._compact, "_summarize_notes", lambda blocks, role: None)  # model down
    for i in range(2):
        md.write(f"N{i}", f"note {i}", kind="note")
    r = md.compact(group_by="kind", summarize=True)
    assert r["summarized"] == []                              # nothing summarized
    body = md.read_file("compacted-note.md")["body"]
    assert "## N0" in body
    assert "## N1" in body


def test_compact_rerun_resummarizes_existing(md, monkeypatch):
    import aiforge_core.memory.md_store as ms
    seen = {"had_prev": False}

    def fake_summarize(blocks, role):
        if any("previous consolidated" in b for b in blocks):
            seen["had_prev"] = True
        return "## C\n\nx"

    monkeypatch.setattr(ms._compact, "_summarize_notes", fake_summarize)
    for i in range(2):
        md.write(f"A{i}", "a", kind="session")
    md.compact(group_by="kind", summarize=True)
    # second round: new notes + the existing consolidated body fed back in
    for i in range(2):
        md.write(f"B{i}", "b", kind="session")
    md.compact(group_by="kind", summarize=True)
    assert seen["had_prev"], "re-compaction must feed the existing body back for re-summary"


def test_compact_concurrent_no_data_loss(md):
    import threading
    for i in range(4):
        md.write(f"S{i}", f"body {i}", kind="session")
    results = []
    def run(): results.append(md.compact(group_by="kind", summarize=False))
    t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(); t2.join()
    files = {f["file"] for f in md.list_files()}
    assert "compacted-session.md" in files
    assert not any(f.startswith("session-") for f in files)   # no orphans
    body = md.read_file("compacted-session.md")["body"]
    assert all(f"## S{i}" in body for i in range(4))           # all notes kept


def test_compact_body_cap_trims_when_merge(md):
    big = "x" * 40000
    for i in range(3):
        md.write(f"S{i}", big, kind="session")
    md.compact(group_by="kind", summarize=False)
    body = md.read_file("compacted-session.md")["body"]
    assert len(body) <= md._COMPACT_BODY_CAP + 200
    assert "older entries trimmed" in body
