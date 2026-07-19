"""compact() asks the sync lease for permission — and fails OPEN when it can't.

Single-machine behaviour must be byte-for-byte what it was before the mesh
existed: no peers configured, no gate.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path


def _approve() -> None:
    from aiforge_core.memory.sync import peers

    data = peers.load()
    data["peers"] = [{"id": "book", "urls": ["http://10.0.0.9:8799"],
                      "state": peers.STATE_APPROVED, "last_seen": 0}]
    peers.save(data)


def _lease_text(tmp_path, text: str) -> None:
    path = tmp_path / "mem" / "okf" / ".lease.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _held_by_book(tmp_path) -> None:
    _lease_text(tmp_path, json.dumps({
        "key": "__lease__", "rev": 1, "holder": "book",
        "expires_at": int(time.time()) + 600}))


def _compact(**kw) -> dict:
    from aiforge_core.memory import md_store

    return md_store.compact(summarize=False, **kw)


def test_compaction_is_skipped_only_when_a_peer_holds_the_lease(mem):
    _approve()
    _held_by_book(mem)

    out = _compact()

    assert out["skipped"] == "lease"
    assert out["files_out"] == 0


def test_compaction_runs_on_a_single_machine_even_with_a_foreign_lease(mem):
    """No approved peers = no mesh = the lease has no say. Unchanged behaviour."""
    _held_by_book(mem)

    out = _compact()

    assert "skipped" not in out


def test_compaction_runs_when_we_hold_the_lease(mem):
    from aiforge_core.memory.sync import lease
    _approve()
    assert lease.claim() is True

    assert "skipped" not in _compact()


def test_a_corrupt_lease_makes_us_compact_anyway_never_skip(mem):
    """Losing compaction to an unreadable file is worse than duplicating it."""
    _approve()
    _lease_text(mem, "{not json at all")

    assert "skipped" not in _compact()


def test_a_lease_check_that_explodes_makes_us_compact_anyway(mem, monkeypatch):
    from aiforge_core.memory.sync import lease
    _approve()
    _held_by_book(mem)
    monkeypatch.setattr(lease, "may_compact",
                        lambda: (_ for _ in ()).throw(OSError("config gone")))

    assert "skipped" not in _compact()


def test_a_dry_run_preview_is_never_gated(mem):
    """It reads, costs no tokens, and an operator asking "what would happen"
    deserves an answer even on a follower."""
    _approve()
    _held_by_book(mem)

    out = _compact(dry_run=True)

    assert out["dry_run"] is True and "skipped" not in out
