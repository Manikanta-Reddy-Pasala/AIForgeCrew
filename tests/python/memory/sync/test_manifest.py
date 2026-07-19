"""Manifest construction from the markdown memory tree."""
from __future__ import annotations

import hashlib


def _seed_capture(root, name: str, text: str) -> None:
    d = root / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_class_a_entries_carry_sha256_of_file_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")

    entries = manifest.build()
    assert len(entries) == 1
    e = entries[0]
    assert e["path"] == "captures/a-20260719-aaaaaa.md"
    assert e["cls"] == "A"
    assert e["hash"] == hashlib.sha256(b"hello").hexdigest()


def test_briefs_are_class_a_too(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    (root / "compacted").mkdir(parents=True, exist_ok=True)
    (root / "compacted" / "compacted-repo.md").write_text("brief", encoding="utf-8")

    paths = {e["path"] for e in manifest.build()}
    assert paths == {"compacted/compacted-repo.md"}


def test_path_for_hash_only_resolves_advertised_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")
    (root / "secret.txt").write_text("nope", encoding="utf-8")

    good = hashlib.sha256(b"hello").hexdigest()
    assert manifest.path_for_hash(good) is not None
    assert manifest.path_for_hash(hashlib.sha256(b"nope").hexdigest()) is None
    assert manifest.path_for_hash("../../etc/passwd") is None
