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


def test_a_symlinked_capture_is_never_advertised(monkeypatch, tmp_path):
    """Path.glob follows symlinks; /blob must not become an arbitrary file reader."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    secret = tmp_path / "secret.txt"
    secret.write_text("classified", encoding="utf-8")
    d = tmp_path / "md" / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / "real-20260719-aaaaaa.md").write_text("fine", encoding="utf-8")
    (d / "evil-20260719-bbbbbb.md").symlink_to(secret)

    paths = {e["path"] for e in manifest.build()}
    assert paths == {"captures/real-20260719-aaaaaa.md"}
    assert manifest.path_for_hash(
        hashlib.sha256(b"classified").hexdigest()) is None


def test_class_b_entry_from_node_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "L-07.md").write_text(
        '---\ntype: learning\nid: "L-07"\norigin: "nuc"\nrev: 47\n'
        'updated_by: "ms"\n---\n\nbody\n',
        encoding="utf-8",
    )

    b = [e for e in manifest.build() if e["cls"] == "B"]
    assert len(b) == 1
    assert b[0]["origin"] == "nuc"
    assert b[0]["key"] == "L-07"
    assert b[0]["rev"] == 47
    assert b[0]["updated_by"] == "ms"


def test_unstamped_node_is_local_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "L-09.md").write_text('---\ntype: learning\nid: "L-09"\n---\n\nbody\n',
                               encoding="utf-8")

    assert [e for e in manifest.build() if e["cls"] == "B"] == []


def test_index_and_conflict_sidecars_never_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / "global" / "learnings").mkdir(parents=True, exist_ok=True)
    (okf / "index.md").write_text("# index\n", encoding="utf-8")
    (okf / "global" / "learnings" / "L-07.conflict.md").write_text("loser\n",
                                                                   encoding="utf-8")

    assert manifest.build() == []


def test_tombstone_and_lease_are_class_b(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / ".tomb" / "nuc").mkdir(parents=True, exist_ok=True)
    (okf / ".tomb" / "nuc" / "L-07.json").write_text(
        '{"origin":"nuc","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}',
        encoding="utf-8",
    )
    (okf / ".lease.json").write_text(
        '{"origin":"","key":"__lease__","rev":3,"updated_by":"nuc",'
        '"holder":"nuc","expires_at":1763000000}',
        encoding="utf-8",
    )

    by_key = {e["key"]: e for e in manifest.build()}
    assert by_key["L-07"]["tomb"] is True
    assert by_key["L-07"]["rev"] == 48
    assert by_key["__lease__"]["rev"] == 3
    assert by_key["__lease__"]["origin"] == ""
