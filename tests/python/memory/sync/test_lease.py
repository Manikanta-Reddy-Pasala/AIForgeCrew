"""The compaction lease. Deliberately not a consensus protocol."""
from __future__ import annotations

import json


def _md(monkeypatch, tmp_path, peer_id: str):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def test_claim_on_an_empty_mesh_writes_the_lease(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    assert lease.claim() is True
    rec = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert rec["holder"] == "nuc"
    assert rec["rev"] == 1
    assert rec["key"] == "__lease__"


def test_holder_is_true_only_for_the_holder(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    assert lease.is_holder() is True

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.is_holder() is False


def test_a_live_lease_cannot_be_stolen(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.claim() is False


def test_an_expired_lease_is_claimable(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    path = tmp_path / "md" / "okf" / ".lease.json"
    rec = json.loads(path.read_text())
    rec["expires_at"] = 1          # far in the past
    path.write_text(json.dumps(rec), encoding="utf-8")

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.claim() is True
    assert json.loads(path.read_text())["holder"] == "book"


def test_claiming_bumps_rev_so_the_lease_merges_like_any_class_b_record(
        monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    path = tmp_path / "md" / "okf" / ".lease.json"
    rec = json.loads(path.read_text())
    rec["expires_at"] = 1
    path.write_text(json.dumps(rec), encoding="utf-8")

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    lease.claim()

    assert json.loads(path.read_text())["rev"] == 2


def test_renew_extends_only_for_the_holder(monkeypatch, tmp_path):
    _md(monkeypatch, tmp_path, "nuc")
    from aiforge_core.memory.sync import lease

    lease.claim()
    before = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert lease.renew() is True
    after = json.loads((tmp_path / "md" / "okf" / ".lease.json").read_text())
    assert after["expires_at"] >= before["expires_at"]

    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    assert lease.renew() is False
