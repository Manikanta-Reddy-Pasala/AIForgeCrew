"""Search link-expansion: a hit brief follows its Links to sibling briefs and
returns their FULL knowledge text (md_store.expand_links + unified_query wiring)."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def _write_brief(mdir, scope, *, facts, links=None):
    from aiforge_core.runtime import work_notes
    note = work_notes.render_note(
        "knowledge", scope, title=scope.title(),
        facts=facts, links=links or [], updated_at="2026-07-12T00:00:00+00:00")
    (mdir / f"compacted-{scope}.md").write_text(note, encoding="utf-8")


def test_expand_links_follows_brief_refs(mem):
    from aiforge_core.memory import md_store
    mdir = md_store.memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    # deploy links to release; release carries a distinct fact
    _write_brief(mdir, "deploy", facts=["Deploy via run.sh restart"],
                 links=["[Release](compacted-release.md)"])
    _write_brief(mdir, "release", facts=["Tag vX triggers Tekton prod build"])

    out = md_store.expand_links(["compacted:compacted-deploy"])
    keys = {o["key"] for o in out}
    assert "release" in keys
    rel = next(o for o in out if o["key"] == "release")
    assert "Tekton" in rel["text"]              # FULL text, not just a ref
    assert rel["source"] == "linked:compacted-release"
    # origin brief itself is excluded from expansion output
    assert "deploy" not in keys


def test_expand_links_excludes_non_brief_refs_and_missing(mem):
    from aiforge_core.memory import md_store
    mdir = md_store.memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    _write_brief(mdir, "svc", facts=["a fact"],
                 links=["https://example.com/doc",
                        "[Ghost](compacted-ghost.md)"])   # ghost file absent
    out = md_store.expand_links(["compacted:compacted-svc"])
    assert out == []                            # url skipped, missing file skipped


def test_unified_query_appends_linked_full_info(mem, monkeypatch):
    from aiforge_core.memory import md_store, unified_query as uq
    mdir = md_store.memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    _write_brief(mdir, "deploy", facts=["Deploy via run.sh restart nucbox"],
                 links=["[Release](compacted-release.md)"])
    _write_brief(mdir, "release",
                 facts=["Tag vX triggers Tekton prod build pipeline"])
    md_store.ingest_dir()

    # limit=1 → only the top-ranked 'deploy' brief is a primary hit; the
    # 'release' brief reaches the result ONLY by following deploy's Links.
    res = uq.query("run.sh restart deploy nucbox", limit=1)
    linked = [h for h in res["hits"] if h.get("linked")]
    assert linked, res["hits"]
    assert "Tekton" in " ".join(h.get("text") or "" for h in linked)
    assert "linked" in res["used_sources"]
