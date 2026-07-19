"""Manifest construction from the markdown memory tree."""
from __future__ import annotations

import hashlib


def _seed_capture(root, name: str, text: str) -> None:
    d = root / "captures"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _node(tmp_path, scope: str, origin: str, key: str, *, rev: int = 1,
          body: str = "b", filename: str | None = None):
    p = tmp_path / "md" / "okf" / scope / "learnings" / (filename or f"{key}.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: {rev}\nupdated_by: "{origin}"\n---\n\n{body}\n',
                 encoding="utf-8")
    return p


def test_class_a_entries_carry_sha256_of_file_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")

    entries = manifest.build()
    assert len(entries) == 1
    e = entries[0]
    assert e["path"] == "captures/a-20260719-aaaaaa.md"
    assert e["kind"] == "A"
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

    b = [e for e in manifest.build() if e["kind"] == "B"]
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

    assert [e for e in manifest.build() if e["kind"] == "B"] == []


def test_index_and_conflict_sidecars_never_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / "global" / "learnings").mkdir(parents=True, exist_ok=True)
    (okf / "index.md").write_text("# index\n", encoding="utf-8")
    (okf / "global" / "learnings" / "L-07.conflict.md").write_text("loser\n",
                                                                   encoding="utf-8")
    # Positive control: an empty manifest would otherwise pass this test even if
    # build() were broken outright.
    _node(tmp_path, "global", "nuc", "L-07")

    assert [e["key"] for e in manifest.build()] == ["L-07"]


def _flat_node(tmp_path, folder: str, origin: str, key: str):
    """A node written straight into one of the top-level directories."""
    p = tmp_path / "md" / folder / origin / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\n'
                 f'rev: 1\nupdated_by: "{origin}"\n---\n\nb\n', encoding="utf-8")
    return p


def test_okf_peers_and_mesh_are_all_advertised(monkeypatch, tmp_path):
    """Each has a different writer, but all three travel: my own knowledge, the
    inbox I received, and the leader's mesh result."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    _node(tmp_path, "global", "book", "L-01")
    _flat_node(tmp_path, "peers", "ms", "L-02")
    _flat_node(tmp_path, "mesh", "nuc", "L-03")

    assert sorted(e["key"] for e in manifest.build()) == ["L-01", "L-02", "L-03"]


def test_the_local_view_is_never_advertised(monkeypatch, tmp_path):
    """The break in the amplification loop: tier-2 output is local-only. If view/
    synced, the leader would fold it into mesh/, it would come back down, and be
    merged into the view again on every round."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    _flat_node(tmp_path, "view", "book", "V-01")
    # Positive control: an empty manifest would pass even if build() were broken.
    _node(tmp_path, "global", "book", "L-01")

    assert [e["key"] for e in manifest.build()] == ["L-01"]


def test_a_tombstone_is_class_b(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / ".tomb" / "nuc").mkdir(parents=True, exist_ok=True)
    (okf / ".tomb" / "nuc" / "L-07.json").write_text(
        '{"origin":"nuc","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}',
        encoding="utf-8",
    )

    by_key = {e["key"]: e for e in manifest.build()}
    assert by_key["L-07"]["tomb"] is True
    assert by_key["L-07"]["rev"] == 48


def test_a_class_b_record_without_an_origin_is_refused(monkeypatch, tmp_path):
    """I5: ('', key) is not an identity — one peer's tombstone would clobber
    another's. Nothing is exempt: the lease used to be, and it is gone."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    okf = tmp_path / "md" / "okf"
    (okf / ".tomb" / "_").mkdir(parents=True, exist_ok=True)
    (okf / ".tomb" / "_" / "L-07.json").write_text(
        '{"origin":"","key":"L-07","rev":48,"updated_by":"nuc","tomb":true}',
        encoding="utf-8",
    )

    assert [e["key"] for e in manifest.build()] == []


def test_an_unaddressable_key_or_origin_never_enters_the_manifest(monkeypatch,
                                                                  tmp_path):
    """B1: a key carrying glob metacharacters would address an unrelated node."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    good = _node(tmp_path, "global", "nuc", "L-07")
    _node(tmp_path, "global", "nuc", "*", filename="star.md")
    _node(tmp_path, "global", "nuc", "../../etc/passwd", filename="dots.md")
    # Non-ASCII keys all sanitise to the same "_" and would overwrite each other.
    _node(tmp_path, "global", "nuc", "日本語", filename="nihongo.md")
    _node(tmp_path, "global", "../..", "L-08", filename="badorigin.md")

    entries = [e for e in manifest.build() if e["kind"] == "B"]
    assert [e["key"] for e in entries] == ["L-07"]
    assert entries[0]["path"] == "okf/global/learnings/" + good.name


def test_symlinked_class_b_records_are_never_advertised(monkeypatch, tmp_path):
    """B2: the symlink guard covered class A only — nodes, tombs and the lease leaked."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    secret = tmp_path / "private.md"
    secret.write_text('---\ntype: learning\nid: "L-99"\norigin: "nuc"\nrev: 1\n'
                      'updated_by: "nuc"\n---\n\nclassified\n', encoding="utf-8")
    secret_json = tmp_path / "private.json"
    secret_json.write_text('{"origin":"nuc","key":"L-98","rev":1,'
                           '"updated_by":"nuc","tomb":true}', encoding="utf-8")

    okf = tmp_path / "md" / "okf"
    (okf / "global" / "learnings").mkdir(parents=True, exist_ok=True)
    (okf / ".tomb" / "nuc").mkdir(parents=True, exist_ok=True)
    (okf / "global" / "learnings" / "L-99.md").symlink_to(secret)
    (okf / ".tomb" / "nuc" / "L-98.json").symlink_to(secret_json)
    (okf / ".lease.json").symlink_to(secret_json)

    assert manifest.build() == []
    for blob in (secret, secret_json):
        assert manifest.path_for_hash(
            hashlib.sha256(blob.read_bytes()).hexdigest()) is None


def test_a_malformed_rev_drops_one_record_not_the_manifest(monkeypatch, tmp_path):
    """B3: int('v2') outside the try 404'd /blob for every file in the tree."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    _seed_capture(tmp_path / "md", "a-20260719-aaaaaa.md", "hello")
    d = tmp_path / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    (d / "L-07.md").write_text('---\ntype: learning\nid: "L-07"\norigin: "nuc"\n'
                               'rev: "v2"\nupdated_by: "nuc"\n---\n\nb\n',
                               encoding="utf-8")

    entries = manifest.build()

    assert {e["hash"] for e in entries} >= {hashlib.sha256(b"hello").hexdigest()}
    assert [e["rev"] for e in entries if e["kind"] == "B"] == [0]
    assert manifest.path_for_hash(hashlib.sha256(b"hello").hexdigest()) is not None


def test_one_identity_in_two_scopes_yields_one_entry(monkeypatch, tmp_path):
    """I1: two entries for one identity made the mesh flip-flop every round."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import manifest

    _node(tmp_path, "global", "nuc", "L-07", rev=3, body="old")
    _node(tmp_path, "projects/x", "nuc", "L-07", rev=9, body="new")

    entries = [e for e in manifest.build() if e["kind"] == "B"]

    assert len(entries) == 1
    assert entries[0]["rev"] == 9
    assert entries[0]["path"] == "okf/projects/x/learnings/L-07.md"


def test_build_is_memoised_but_never_stale(monkeypatch, tmp_path):
    """The cache must not cost correctness: a sync resolves one hash per blob.

    Uncached, every /blob request re-hashed the whole tree — n full scans and
    n x m hashes to serve n files. Cached, a repeat build does no hashing at
    all, and any add / edit / delete invalidates it.
    """
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    from aiforge_core.memory.sync import _io, manifest

    root = tmp_path / "md"
    _seed_capture(root, "a-20260719-aaaaaa.md", "hello")
    first = manifest.build()

    hashed: list = []
    real = _io.sha256_file
    monkeypatch.setattr(_io, "sha256_file", lambda p: (hashed.append(p), real(p))[1])

    assert manifest.build() == first
    assert hashed == []                       # served from the memo

    _seed_capture(root, "b-20260719-bbbbbb.md", "second")
    assert len(manifest.build()) == 2         # an added file invalidates
    (root / "captures" / "a-20260719-aaaaaa.md").write_text("edited", encoding="utf-8")
    assert (manifest.path_for_hash(hashlib.sha256(b"edited").hexdigest())
            is not None)                      # an edit does too
    assert manifest.path_for_hash(hashlib.sha256(b"hello").hexdigest()) is None
    (root / "captures" / "b-20260719-bbbbbb.md").unlink()
    assert len(manifest.build()) == 1         # and so does a delete


def test_the_cache_is_not_shared_between_two_trees(monkeypatch, tmp_path):
    """Two peers in one process must not see each other's manifest."""
    from aiforge_core.memory.sync import manifest

    for name, text in (("one", "from one"), ("two", "from two")):
        monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / name / "md"))
        _seed_capture(tmp_path / name / "md", f"{name[0]}-20260719-aaaaaa.md", text)

    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "one" / "md"))
    assert [e["path"] for e in manifest.build()] == ["captures/o-20260719-aaaaaa.md"]
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "two" / "md"))
    assert [e["path"] for e in manifest.build()] == ["captures/t-20260719-aaaaaa.md"]
