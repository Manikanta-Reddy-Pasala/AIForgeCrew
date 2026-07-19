"""Local deletion must be expressible to the mesh, not just to the filesystem."""
from __future__ import annotations

import json


def _env(monkeypatch, tmp_path, peer_id: str = "book"):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _node(tmp_path, origin: str, key: str, rev: int):
    p = tmp_path / "md" / "okf" / "global" / "learnings" / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: {rev}\nupdated_by: "{origin}"\n---\n\nbody\n',
                 encoding="utf-8")
    return p


def test_delete_removes_the_node_and_leaves_a_tombstone(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import tombstone

    node = _node(tmp_path, "nuc", "L-07", 47)

    assert tombstone.delete_node("nuc", "L-07") is True
    assert not node.exists()

    rec = json.loads((tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json")
                     .read_text(encoding="utf-8"))
    assert rec == {"origin": "nuc", "key": "L-07", "rev": 48,
                   "updated_by": "book", "tomb": True}


def test_tombstone_rev_beats_the_node_it_replaced(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import merge, tombstone

    _node(tmp_path, "nuc", "L-07", 47)
    tombstone.delete_node("nuc", "L-07")

    from aiforge_core.memory.sync import manifest
    local = [{"path": "x", "hash": "h", "cls": "B", "origin": "nuc",
              "key": "L-07", "rev": 47, "updated_by": "nuc"}]
    remote = manifest.build()

    # A peer still holding rev 47 must accept the tombstone.
    assert merge.plan_sync(local, remote)["want"][0]["tomb"] is True


def test_deleting_an_unknown_identity_is_a_no_op(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import tombstone

    assert tombstone.delete_node("nuc", "L-99") is False
    assert not (tmp_path / "md" / "okf" / ".tomb").exists()
