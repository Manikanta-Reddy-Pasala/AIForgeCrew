"""`may_compact` — the one place the "am I allowed to compact?" policy lives.

Compaction is LLM-expensive and non-deterministic, so exactly one peer in a mesh
should run it. A machine with no mesh must be unaffected.
"""
from __future__ import annotations

import json
import time


def _env(monkeypatch, tmp_path, peer_id: str = "nuc"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _approve(peer_id: str = "book") -> None:
    from aiforge_core.memory.sync import peers

    data = peers.load()
    data["peers"] = [{"id": peer_id, "urls": ["http://10.0.0.9:8799"],
                      "state": peers.STATE_APPROVED, "last_seen": 0}]
    peers.save(data)


def _lease_held_by(tmp_path, holder: str, *, expires_in: int = 600) -> None:
    path = tmp_path / "md" / "okf" / ".lease.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "key": "__lease__", "rev": 1, "holder": holder, "updated_by": holder,
        "expires_at": int(time.time()) + expires_in,
    }), encoding="utf-8")


def test_no_peers_configured_means_always_free_to_compact(monkeypatch, tmp_path):
    """The default install. A lease held by somebody else is irrelevant here."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import lease

    assert lease.may_compact() is True
    _lease_held_by(tmp_path, "book")
    assert lease.may_compact() is True


def test_the_holder_may_compact(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease

    assert lease.claim() is True
    assert lease.may_compact() is True


def test_another_peers_live_lease_blocks_us(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease

    _lease_held_by(tmp_path, "book")

    assert lease.may_compact() is False
    assert lease.holder() == "book"


def test_an_expired_lease_unblocks_us_again(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease

    _lease_held_by(tmp_path, "book", expires_in=-1)

    assert lease.may_compact() is True
    assert lease.holder() == ""


def test_no_lease_at_all_does_not_block(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _approve()
    from aiforge_core.memory.sync import lease

    assert lease.may_compact() is True
