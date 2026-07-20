"""OKF node dedupe is leader-only, by the same election as compaction.

A concept-similarity merge is non-deterministic, so two peers each running
``--dedupe`` by hand would fold the same shared knowledge two different ways.
Same policy helper, same soft-fail direction (OPEN).
"""
from __future__ import annotations

import importlib
import time

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    import aiforge_core.memory.okf.store as s
    return importlib.reload(s)


def _approve(peer_id: str) -> None:
    from aiforge_core.memory.sync import peers

    peers.save({"self": {"id": "nuc"}, "peers": [
        {"id": peer_id, "urls": ["http://10.0.0.9:8799"],
         "state": peers.STATE_APPROVED, "last_seen": int(time.time()) - 60}]})


def _dupes(store) -> None:
    meta = {"scope": "global"}
    store.save_node("learning", "L-01", meta, "Always paginate jira_search calls")
    store.save_node("learning", "L-07", meta, "Always paginate the jira_search calls")


def _learning_files(store) -> int:
    return len([d for d in store.load_all() if d.get("type") == "learning"])


def test_a_non_leader_skips_and_says_who_leads(store):
    _approve("air")                    # 'air' < 'nuc'
    _dupes(store)

    res = store.dedupe_nodes()

    assert res == {"ok": True, "removed": 0, "kept": 0,
                   "skipped": "not-leader", "leader": "air"}
    assert _learning_files(store) == 2, "a follower must not touch the tree"


def test_the_leader_dedupes(store):
    _approve("zed")                    # 'nuc' < 'zed', so we lead
    _dupes(store)

    res = store.dedupe_nodes()

    assert res["removed"] == 1 and "skipped" not in res
    assert _learning_files(store) == 1


def test_a_single_machine_dedupes_exactly_as_before(store):
    _dupes(store)

    res = store.dedupe_nodes()

    assert res["removed"] == 1 and "skipped" not in res


def test_a_broken_election_dedupes_anyway(store, monkeypatch):
    from aiforge_core.memory.sync import election
    _approve("air")
    _dupes(store)
    monkeypatch.setattr(election, "is_leader",
                        lambda: (_ for _ in ()).throw(OSError("config gone")))

    assert store.dedupe_nodes()["removed"] == 1
