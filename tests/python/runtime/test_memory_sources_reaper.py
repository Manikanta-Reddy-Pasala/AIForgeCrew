"""memory_sources index guard + stale-indexing reaper.

A source stuck ``indexing`` (crashed ingest thread never clears its status)
is never re-indexable; a double-click / concurrent index endpoint spawns two
ingest threads over the same source. ``claim_for_index`` makes the flip atomic
(idle->indexing) and refuses a second concurrent claim; ``reap_stale_indexing``
requeues sources stuck past the lease back to ``idle``."""
import importlib

import pytest


@pytest.fixture
def ms(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SOURCES_DB_PATH", str(tmp_path / "sources.db"))
    from aiforge_core.runtime import memory_sources as m
    importlib.reload(m)
    return m


def _backdate_indexing(ms, source_id, seconds_ago):
    with ms._conn() as c:
        c.execute(
            "UPDATE memory_sources SET indexing_started_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now',?) WHERE id = ?",
            (f"-{int(seconds_ago)} seconds", source_id),
        )


def test_claim_for_index_atomic_single_winner(ms):
    src = ms.create("repo", "/tmp/repo-x")
    assert ms.claim_for_index(src["id"]) is True     # idle -> indexing
    assert ms.get(src["id"])["status"] == "indexing"
    assert ms.claim_for_index(src["id"]) is False    # already indexing


def test_claim_after_done_allowed(ms):
    src = ms.create("repo", "/tmp/repo-y")
    assert ms.claim_for_index(src["id"]) is True
    ms.set_status(src["id"], "done", units=3, indexed=True)
    assert ms.claim_for_index(src["id"]) is True     # done -> indexing again


def test_reap_stale_indexing_resets(ms):
    src = ms.create("repo", "/tmp/repo-z")
    assert ms.claim_for_index(src["id"]) is True
    _backdate_indexing(ms, src["id"], 3600)          # 1h old

    reset = list(ms.reap_stale_indexing(1800))       # 30m lease
    assert src["id"] in reset
    assert ms.get(src["id"])["status"] == "idle"


def test_reap_leaves_fresh_indexing(ms):
    src = ms.create("repo", "/tmp/repo-fresh")
    assert ms.claim_for_index(src["id"]) is True     # started now
    reset = list(ms.reap_stale_indexing(1800))
    assert src["id"] not in reset
    assert ms.get(src["id"])["status"] == "indexing"
