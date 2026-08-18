"""Local compaction only reaches the other machines through OKF nodes.

Every machine compacts its own captures into ``compacted/`` briefs, and those
briefs are class A files that never travel — each machine has its own. So a fact
that never becomes an OKF node never leaves the box it was learned on. This
conversion is the bridge, and it runs on every cycle rather than once as a
migration, which is the difference these tests pin down.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    return tmp_path


def _brief(topic: str, facts: list[str]) -> None:
    from aiforge_core.memory import md_store

    body = "".join(f"- {f}\n" for f in facts)
    (md_store.briefs_dir() / f"compacted-{topic}.md").write_text(
        f"---\ntype: knowledge\nkind: knowledge\nkey: {topic}\nresource: ''\n"
        f"timestamp: 2026-08-18T00:00:00Z\ntags: []\nlinks: []\n---\n\n"
        f"# {topic}\n\n## Facts\n\n{body}", encoding="utf-8")


def _learnings() -> list[dict]:
    from aiforge_core.memory.okf import store

    return [d for d in store.load_all() if d.get("type") == "learning"]


def test_a_brief_becomes_a_node_that_can_travel(mem):
    from aiforge_core.memory.okf import author

    _brief("redis", ["evictions need maxmemory-policy allkeys-lru"])

    out = author.sync_briefs_to_nodes()

    assert out["created"] == 1
    node = _learnings()[0]
    assert "allkeys-lru" in node["body"]
    # It is a real OKF node under okf/, i.e. exactly what push offers upstream.
    assert (mem / "mem" / "okf").is_dir()


def test_a_second_pass_over_unchanged_briefs_writes_nothing(mem):
    """No write means no ``rev`` bump, which means nothing to re-sync: an idle
    machine must not push the same node every cycle forever."""
    from aiforge_core.memory.okf import author

    _brief("redis", ["evictions need maxmemory-policy allkeys-lru"])
    author.sync_briefs_to_nodes()
    before = (mem / "mem" / "okf").rglob("*.md")
    stamps = {p: p.read_bytes() for p in before}

    out = author.sync_briefs_to_nodes()

    assert (out["created"], out["updated"]) == (0, 0)
    assert {p: p.read_bytes() for p in stamps} == stamps


def test_a_new_fact_on_an_existing_topic_updates_the_node(mem):
    """The migration this replaces SKIPPED a topic it had already seen, so every
    fact learned after the first run stayed local forever."""
    from aiforge_core.memory.okf import author

    _brief("redis", ["evictions need maxmemory-policy allkeys-lru"])
    author.sync_briefs_to_nodes()

    _brief("redis", ["evictions need maxmemory-policy allkeys-lru",
                     "keyspace notifications need notify-keyspace-events Kx"])
    out = author.sync_briefs_to_nodes()

    assert (out["created"], out["updated"]) == (0, 1)
    nodes = _learnings()
    assert len(nodes) == 1, "one topic must stay one node"
    assert "allkeys-lru" in nodes[0]["body"]
    assert "notify-keyspace-events" in nodes[0]["body"]


def test_the_updated_node_carries_a_higher_rev(mem):
    """Sync orders versions by ``rev``: an update that did not bump it would be
    refused as stale by every machine that already holds the node."""
    from aiforge_core.memory.okf import author
    from aiforge_core.memory.sync import merge

    _brief("redis", ["fact one"])
    author.sync_briefs_to_nodes()
    first = merge.as_rev((_learnings()[0].get("meta") or {}).get("rev"))

    _brief("redis", ["fact one", "fact two"])
    author.sync_briefs_to_nodes()

    assert merge.as_rev((_learnings()[0].get("meta") or {}).get("rev")) > first


def test_the_cycle_runs_the_conversion(mem, monkeypatch):
    """It is wired into ``run_after_sync``, before both tiers, so this cycle's
    compaction output is in okf/ in time for this cycle's fold and next push."""
    from aiforge_core.memory.okf import tiers

    _brief("redis", ["evictions need maxmemory-policy allkeys-lru"])
    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate",
                        lambda *a, **k: {"objective": "", "key_results": [],
                                         "facts": [], "links": [],
                                         "learnings": []})

    out = tiers.run_after_sync()

    assert out["briefs"]["created"] == 1
    assert any("allkeys-lru" in n["body"] for n in _learnings())


def test_a_conversion_that_explodes_does_not_kill_the_cycle(mem, monkeypatch):
    from aiforge_core.memory.okf import author, tiers

    monkeypatch.setattr(author, "sync_briefs_to_nodes",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))

    out = tiers.run_after_sync()

    assert out["briefs"]["ok"] is False
    assert "mesh" in out and "view" in out, "the tiers still ran"
