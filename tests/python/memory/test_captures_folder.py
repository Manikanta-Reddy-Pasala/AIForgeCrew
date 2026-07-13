"""Raw captures live in the captures/ subfolder; migration is non-destructive."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_EMBED_BACKEND", "hash")
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return tmp_path


def test_write_lands_in_captures_folder_not_root(mem):
    from aiforge_core.memory import md_store as m
    m.write("H2-2 subnet note", "H2-2 is on 10.130.112.x", kind="note")
    caps = list(m.captures_dir().glob("*.md"))
    assert len(caps) == 1
    # nothing loose in the root
    assert list(m.memory_dir().glob("*.md")) == []
    assert caps[0].parent == m.captures_dir()


def test_read_resolves_a_capture_in_folder(mem):
    from aiforge_core.memory import md_store as m
    m.write("A note", "body text here", kind="note")
    name = list(m.captures_dir().glob("*.md"))[0].name
    got = m.read_file(name)
    assert got and "body text here" in got["body"]


def test_migrate_moves_legacy_root_captures(mem):
    from aiforge_core.memory import md_store as m
    # legacy layout: raw captures + a brief sitting in the root
    (m.memory_dir() / "ssh-key-location-20260713-abc123.md").write_text(
        "---\nkind: note\n---\nssh key at ~/sshkey", encoding="utf-8")
    (m.memory_dir() / "thinking-process.md").write_text(
        "---\nkind: note\n---\nleftover", encoding="utf-8")
    (m.memory_dir() / "compacted-shared.md").write_text(
        "---\nkind: knowledge\n---\na brief", encoding="utf-8")

    r = m.migrate_captures_to_folder()
    assert r["moved"] == 2                                    # 2 captures, NOT the brief
    assert (m.captures_dir() / "ssh-key-location-20260713-abc123.md").exists()
    assert (m.captures_dir() / "thinking-process.md").exists()
    # brief untouched in root (handled by the briefs migration, not this one)
    assert (m.memory_dir() / "compacted-shared.md").exists()
    # no loose captures left in the root
    assert not (m.memory_dir() / "thinking-process.md").exists()


def test_migrate_captures_is_idempotent_and_dedups(mem):
    from aiforge_core.memory import md_store as m
    (m.memory_dir() / "a-20260713-aaaaaa.md").write_text("x", encoding="utf-8")
    assert m.migrate_captures_to_folder()["moved"] == 1
    # a duplicate reappears in the root → migration drops it, keeps the foldered one
    (m.memory_dir() / "a-20260713-aaaaaa.md").write_text("x", encoding="utf-8")
    assert m.migrate_captures_to_folder()["moved"] == 1
    assert (m.captures_dir() / "a-20260713-aaaaaa.md").exists()
    assert not (m.memory_dir() / "a-20260713-aaaaaa.md").exists()
    # nothing left to move
    assert m.migrate_captures_to_folder()["moved"] == 0


def test_clear_wipes_captures_folder(mem):
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory import admin
    m.write("cap one", "body one", kind="note")
    assert list(m.captures_dir().glob("*.md"))
    admin.clear_store("md_files")
    assert list(m.captures_dir().glob("*.md")) == []
    assert list(m.memory_dir().glob("*.md")) == []


def test_startup_migration_clears_loose_captures_from_root(mem):
    """The user's actual ask: no loose capture .md left in the memory-dir root
    after startup — it's moved into captures/ (or consumed onward by compaction),
    never left beside the folders."""
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory import migrations
    (m.memory_dir() / "legacy-cap-20260713-ffffff.md").write_text(
        "---\nkind: note\n---\nold capture", encoding="utf-8")
    migrations.run_startup_migrations()
    assert not (m.memory_dir() / "legacy-cap-20260713-ffffff.md").exists()
    # only folders + markers remain at the root (no loose *.md)
    assert list(m.memory_dir().glob("*.md")) == []
