import importlib

import pytest


@pytest.fixture
def ms(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "src.db"))
    import aiforge_core.runtime.memory_sources as ms
    importlib.reload(ms)
    return ms


def test_create_list_delete(ms):
    s = ms.create("repo", "/some/path", "myrepo")
    assert s["kind"] == "repo"
    assert s["name"] == "myrepo"
    assert s["status"] == "idle"
    assert len(ms.list_sources()) == 1
    ms.set_status(s["id"], "done", units=42, indexed=True)
    got = ms.get(s["id"])
    assert got["status"] == "done"
    assert got["units"] == 42
    assert got["last_indexed"]
    assert ms.delete(s["id"]) is True
    assert ms.list_sources() == []


def test_bad_kind(ms):
    with pytest.raises(ValueError):
        ms.create("bogus", "/x")


def test_ingest_repo(monkeypatch, tmp_path):
    # fake repo with code + md + a noise dir
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def f():\n    return 1\n" * 50)
    (repo / "README.md").write_text("# Title\n" + "doc line\n" * 50)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.py").write_text("should be skipped\n")

    written = []
    import aiforge_core.runtime.tools.memory_write as mw
    monkeypatch.setattr(mw, "memory_write",
                        lambda **kw: written.append(kw) or {"ok": True, "id": len(written)})

    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    res = mi.ingest_source({"kind": "repo", "name": "repo", "location": str(repo)})
    assert res["error"] is None
    assert res["units"] > 0
    refs = [w["tags"][-1] for w in written]
    assert any("a.py" in r for r in refs)
    assert any("README.md" in r for r in refs)
    assert not any("junk.py" in r for r in refs)   # noise dir skipped


def test_ingest_missing_dir(tmp_path):
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)
    res = mi.ingest_source({"kind": "repo", "name": "x",
                            "location": str(tmp_path / "nope")})
    assert res["units"] == 0
    assert "not a directory" in res["error"]


def test_run_index_updates_status(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "s.db"))
    import aiforge_core.runtime.memory_sources as ms
    importlib.reload(ms)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "n.md").write_text("# note\n" + "x\n" * 80)

    import aiforge_core.runtime.tools.memory_write as mw
    monkeypatch.setattr(mw, "memory_write", lambda **kw: {"ok": True, "id": 1})
    import aiforge_core.runtime.memory_ingest as mi
    importlib.reload(mi)

    s = ms.create("docs", str(docs), "docs")
    mi.run_index(s["id"])
    got = ms.get(s["id"])
    assert got["status"] == "done"
    assert got["units"] >= 1


def test_fresh_index_not_reaped_then_stale_is(ms):
    sid = ms.create("repo", "/p", "r")["id"]
    # Entering 'indexing' stamps a fresh lease clock → a sane lease won't reap.
    ms.set_status(sid, "indexing")
    assert ms.reap_stale_indexing(1800) == []
    assert ms.get(sid)["status"] == "indexing"
    # Backdate the clock past the lease → reaped to idle with the UI message.
    with ms._conn() as c:
        c.execute(
            "UPDATE memory_sources SET indexing_started_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ','now','-3600 seconds') WHERE id=?",
            (sid,))
    assert ms.reap_stale_indexing(1800) == [sid]
    got = ms.get(sid)
    assert got["status"] == "idle"
    assert "exceeded lease" in (got["error"] or "")


def test_heartbeat_protects_slow_index(ms):
    sid = ms.create("repo", "/p", "r")["id"]
    ms.set_status(sid, "indexing")
    with ms._conn() as c:
        c.execute(
            "UPDATE memory_sources SET indexing_started_at="
            "strftime('%Y-%m-%dT%H:%M:%fZ','now','-3600 seconds') WHERE id=?",
            (sid,))
    # Heartbeat refreshes the clock → the slow-but-alive index survives the reap.
    ms.touch_indexing(sid)
    assert ms.reap_stale_indexing(1800) == []
    assert ms.get(sid)["status"] == "indexing"
    # touch_indexing is a no-op once the source is no longer 'indexing'.
    ms.set_status(sid, "done", indexed=True)
    ms.touch_indexing(sid)
    assert ms.get(sid)["status"] == "done"
