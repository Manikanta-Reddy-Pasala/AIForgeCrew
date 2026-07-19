"""Applying a fetched blob to the local tree: verify, place, preserve."""
from __future__ import annotations

import hashlib


def _md(tmp_path):
    d = tmp_path / "md"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_apply_writes_a_class_a_blob(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    body = b"hello"
    entry = {"path": "captures/a.md", "hash": hashlib.sha256(body).hexdigest(),
             "kind": "A"}

    assert apply.apply_blob(entry, body) is True
    assert (tmp_path / "md" / "captures" / "a.md").read_bytes() == body


def test_apply_rejects_a_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    entry = {"path": "captures/a.md", "hash": "0" * 64, "kind": "A"}

    assert apply.apply_blob(entry, b"tampered") is False
    assert not (tmp_path / "md" / "captures" / "a.md").exists()


def test_apply_refuses_to_escape_the_memory_tree(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    body = b"pwn"
    entry = {"path": "../../.ssh/authorized_keys",
             "hash": hashlib.sha256(body).hexdigest(), "kind": "A"}

    assert apply.apply_blob(entry, body) is False
    assert not (tmp_path / ".ssh").exists()


def test_applying_a_tombstone_removes_the_node(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
                    'updated_by: "nuc"\n---\n\nbody\n', encoding="utf-8")

    body = b'{"origin":"nuc","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}'
    entry = {"path": "okf/.tomb/nuc/L-07.json",
             "hash": hashlib.sha256(body).hexdigest(), "kind": "B",
             "origin": "nuc", "key": "L-07", "rev": 48, "updated_by": "nuc",
             "tomb": True}

    assert apply.apply_blob(entry, body) is True
    assert not node.exists()
    assert (tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json").exists()


def test_applying_a_node_removes_its_tombstone(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    tomb = tmp_path / "md" / "okf" / ".tomb" / "nuc" / "L-07.json"
    tomb.parent.mkdir(parents=True, exist_ok=True)
    tomb.write_text('{"origin":"nuc","key":"L-07","rev":48,'
                    '"updated_by":"nuc","tomb":true}', encoding="utf-8")

    body = (b'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 49\n'
            b'updated_by: "ms"\n---\n\nnew\n')
    entry = {"path": "okf/global/learnings/L-07.md",
             "hash": hashlib.sha256(body).hexdigest(), "kind": "B",
             "origin": "nuc", "key": "L-07", "rev": 49, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert not tomb.exists()


def test_foreign_node_lands_under_peers_not_over_a_local_id(monkeypatch, tmp_path):
    """(nuc, O-01) and (ms, O-01) are different objects with the same filename."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import apply

    mine = tmp_path / "md" / "okf" / "global" / "objectives" / "O-01.md"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text('---\ntype: objective\nid: "O-01"\norigin: "book"\nrev: 3\n'
                    'updated_by: "book"\n---\n\nmine\n', encoding="utf-8")

    body = (b'---\ntype: objective\nid: "O-01"\norigin: "ms"\nrev: 1\n'
            b'updated_by: "ms"\n---\n\ntheirs\n')
    entry = {"path": "okf/global/objectives/O-01.md",
             "hash": hashlib.sha256(body).hexdigest(), "kind": "B",
             "origin": "ms", "key": "O-01", "rev": 1, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert "mine" in mine.read_text(encoding="utf-8")          # untouched
    assert (tmp_path / "md" / "okf" / "peers" / "ms" / "O-01.md").exists()


def test_an_update_to_an_existing_identity_lands_in_place(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
                    'updated_by: "nuc"\n---\n\nold\n', encoding="utf-8")

    body = (b'---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 48\n'
            b'updated_by: "ms"\n---\n\nnew\n')
    entry = {"path": "okf/peers/nuc/L-07.md",   # the peer's own layout differs
             "hash": hashlib.sha256(body).hexdigest(), "kind": "B",
             "origin": "nuc", "key": "L-07", "rev": 48, "updated_by": "ms"}

    assert apply.apply_blob(entry, body) is True
    assert "new" in node.read_text(encoding="utf-8")           # updated in place
    assert not (tmp_path / "md" / "okf" / "peers" / "nuc" / "L-07.md").exists()


def test_conflict_writes_a_sidecar_beside_the_loser(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(_md(tmp_path)))
    from aiforge_core.memory.sync import apply

    node = tmp_path / "md" / "okf" / "global" / "learnings" / "L-07.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("local version\n", encoding="utf-8")

    apply.keep_conflict({"path": "okf/global/learnings/L-07.md",
                          "key": "L-07", "updated_by": "alice"})

    sidecar = node.parent / "L-07.conflict.md"
    assert sidecar.read_text(encoding="utf-8") == "local version\n"
