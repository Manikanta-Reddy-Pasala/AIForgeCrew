"""OKF node dedupe is local, on every machine.

It only ever collapses nodes this machine minted — ``tombstone.mark_deleted``
refuses another origin — so there is nothing for the admin to arbitrate. The
cross-machine merge is the separate, admin-only step.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    monkeypatch.delenv("AIFORGE_ADMIN_ID", raising=False)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    import aiforge_core.memory.okf.store as s
    return importlib.reload(s)


def _dupes(store) -> None:
    meta = {"scope": "global"}
    store.save_node("learning", "L-01", meta, "Always paginate jira_search calls")
    store.save_node("learning", "L-07", meta, "Always paginate the jira_search calls")


def _learning_files(store) -> int:
    return len([d for d in store.load_all() if d.get("type") == "learning"])


def test_a_spoke_dedupes_its_own_nodes(store, monkeypatch):
    monkeypatch.setenv("AIFORGE_ADMIN_URL", "http://10.0.0.9:8799")
    monkeypatch.setenv("AIFORGE_ADMIN_ID", "air")
    _dupes(store)

    res = store.dedupe_nodes()

    assert res["removed"] == 1
    assert "skipped" not in res
    assert _learning_files(store) == 1


def test_the_admin_dedupes(store):
    _dupes(store)

    res = store.dedupe_nodes()

    assert res["removed"] == 1
    assert "skipped" not in res
    assert _learning_files(store) == 1


def test_a_single_machine_dedupes_exactly_as_before(store):
    _dupes(store)

    res = store.dedupe_nodes()

    assert res["removed"] == 1
    assert "skipped" not in res


def test_a_garbage_role_dedupes_anyway(store, monkeypatch):
    """Nothing about the role reaches local dedupe — not even a broken one."""
    _dupes(store)
    monkeypatch.setenv("AIFORGE_ROLE", "leader")

    assert store.dedupe_nodes()["removed"] == 1
