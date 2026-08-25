"""OKF migration: legacy frontmatter keys (kind/source_url/updated_at/
created_at) are rewritten to OKF names (type/resource/timestamp) on disk,
idempotently, without touching bodies or reserved index.md/log.md files."""
from __future__ import annotations

import pytest

from aiforge_core.memory import migrations


def test_rewrite_file_frontmatter_renames_legacy_keys(tmp_path):
    p = tmp_path / "compacted-svc.md"
    p.write_text(
        "---\nkind: knowledge\nkey: svc\nsource_url: https://x/y\n"
        "updated_at: 2020-01-01T00:00:00+00:00\ntags: []\n---\n"
        "# svc\n\n## Facts\n\n- port 8090 (source_url in body stays)\n",
        encoding="utf-8")
    assert migrations._rewrite_file_frontmatter_to_okf(p) is True
    txt = p.read_text(encoding="utf-8")
    head = txt.split("\n---", 1)[0]
    assert "type: knowledge" in head
    assert "resource: https://x/y" in head
    assert "timestamp: 2020-01-01" in head
    assert "kind:" not in head
    assert "source_url:" not in head
    assert "updated_at:" not in head
    # body is untouched (the "source_url" word in prose is preserved)
    assert "source_url in body stays" in txt
    # idempotent: a second pass makes no change
    assert migrations._rewrite_file_frontmatter_to_okf(p) is False


def test_rewrite_created_at_folds_to_timestamp(tmp_path):
    p = tmp_path / "O-01.md"
    p.write_text("---\ntype: objective\nid: O-01\n"
                 "created_at: 2021-05-05T00:00:00+00:00\n---\n# goal\n",
                 encoding="utf-8")
    assert migrations._rewrite_file_frontmatter_to_okf(p) is True
    head = p.read_text(encoding="utf-8").split("\n---", 1)[0]
    assert "timestamp: 2021-05-05" in head
    assert "created_at:" not in head


def test_rewrite_skips_when_okf_key_already_present(tmp_path):
    # a file with BOTH type and a stray kind must not double-write type
    p = tmp_path / "x.md"
    p.write_text("---\ntype: learning\nkind: learning\nid: L-1\n---\nb\n",
                 encoding="utf-8")
    migrations._rewrite_file_frontmatter_to_okf(p)
    head = p.read_text(encoding="utf-8").split("\n---", 1)[0]
    assert head.count("type:") == 1        # not duplicated


def test_migrate_frontmatter_walks_compacted_and_skips_reserved(
        tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    (mem / "compacted").mkdir(parents=True)
    (mem / "compacted" / "compacted-a.md").write_text(
        "---\nkind: knowledge\nkey: a\n---\n# a\n", encoding="utf-8")
    # reserved OKF files have no frontmatter and must be left alone
    (mem / "compacted" / "index.md").write_text("# nav\n- a\n", encoding="utf-8")
    monkeypatch.setattr("aiforge_core.memory.md_store.memory_dir",
                        lambda: mem)
    r = migrations._migrate_frontmatter_to_okf()
    assert r["ok"]
    assert r["rewritten"] == 1
    a = (mem / "compacted" / "compacted-a.md").read_text(encoding="utf-8")
    assert "type: knowledge" in a
    assert "kind:" not in a.split("\n---", 1)[0]
    assert (mem / "compacted" / "index.md").read_text(encoding="utf-8") \
        == "# nav\n- a\n"                  # reserved file untouched


def test_rename_removes_stale_marker_only_okr(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    (mem / "okr").mkdir(parents=True)
    (mem / "okr" / ".migrations.json").write_text("{}", encoding="utf-8")
    (mem / "okf").mkdir()                  # okf/ already the live bundle
    monkeypatch.setattr("aiforge_core.memory.md_store.memory_dir", lambda: mem)
    r = migrations._rename_okr_dir_to_okf()
    assert "removed_stale_marker" in r
    assert not (mem / "okr").exists()      # orphaned marker folder gone


def test_rename_moves_okr_when_okf_absent(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    (mem / "okr" / "global" / "learnings").mkdir(parents=True)
    (mem / "okr" / "global" / "learnings" / "L-01.md").write_text(
        "---\ntype: learning\nid: L-01\n---\nx\n", encoding="utf-8")
    monkeypatch.setattr("aiforge_core.memory.md_store.memory_dir", lambda: mem)
    r = migrations._rename_okr_dir_to_okf()
    assert r.get("ok")
    assert not (mem / "okr").exists()
    assert (mem / "okf" / "global" / "learnings" / "L-01.md").is_file()
