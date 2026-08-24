"""Foreign nodes move out of okf/ and into the top-level peers/ inbox.

An existing install has other peers' raw nodes at ``okf/peers/<origin>/``, which
is exactly the tree compaction reads as "my knowledge". The migration relocates
them once, on startup, without ever losing one.
"""
from __future__ import annotations


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))


def _legacy(tmp_path, origin: str, key: str, body: str = "old"):
    p = tmp_path / "md" / "okf" / "peers" / origin / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_legacy_foreign_nodes_move_to_the_inbox(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory import migrations

    _legacy(tmp_path, "nuc", "L-07")

    assert migrations._move_okf_peers_to_inbox()["moved"] == 1

    assert (tmp_path / "md" / "peers" / "nuc" / "L-07.md").read_text(
        encoding="utf-8") == "old"
    assert not (tmp_path / "md" / "okf" / "peers").exists()


def test_running_it_twice_is_a_no_op(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory import migrations

    _legacy(tmp_path, "nuc", "L-07")
    migrations._move_okf_peers_to_inbox()
    second = migrations._move_okf_peers_to_inbox()

    assert second["ok"]
    assert second.get("skipped")
    assert (tmp_path / "md" / "peers" / "nuc" / "L-07.md").exists()


def test_a_collision_leaves_the_destination_intact_and_keeps_the_source(
        monkeypatch, tmp_path):
    """Half-migrated trees exist. The newer layout wins; nothing is deleted."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory import migrations

    _legacy(tmp_path, "nuc", "L-07", body="legacy")
    dest = tmp_path / "md" / "peers" / "nuc" / "L-07.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("current", encoding="utf-8")

    out = migrations._move_okf_peers_to_inbox()

    assert out == {"ok": True, "moved": 0, "kept_at_destination": 1}
    assert dest.read_text(encoding="utf-8") == "current"
    assert (tmp_path / "md" / "okf" / "peers" / "nuc" / "L-07.md").read_text(
        encoding="utf-8") == "legacy"


def test_an_absent_legacy_folder_is_not_an_error(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory import migrations

    assert migrations._move_okf_peers_to_inbox()["ok"] is True


def test_the_migrated_node_is_advertised_from_its_new_home(monkeypatch, tmp_path):
    """End to end: after the move the node is still part of the mesh."""
    _env(monkeypatch, tmp_path)
    from aiforge_core.memory import migrations
    from aiforge_core.memory.sync import manifest

    _legacy(tmp_path, "nuc", "L-07",
            body='---\ntype: learning\nid: "L-07"\norigin: "nuc"\n'
                 'rev: 1\nupdated_by: "nuc"\n---\n\nb\n')
    migrations._move_okf_peers_to_inbox()

    entries = manifest.build()
    assert [(e["key"], e["path"]) for e in entries] == [("L-07", "peers/nuc/L-07.md")]
