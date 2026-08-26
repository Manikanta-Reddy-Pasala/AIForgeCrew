"""Snapshots and revert.

Hardlinks, so a snapshot of a tree of markdown notes costs inodes and no bytes —
which is what makes "snapshot before every fold" affordable, and affordability
is the whole design. A snapshot somebody has to remember to take is one nobody
has.
"""
from __future__ import annotations

import pytest

from aiforge_core.memory.sync import _io, snapshot


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.delenv("AIFORGE_SYNC_SNAPSHOTS", raising=False)
    root = _io.root()
    (root / "mesh" / "nuc").mkdir(parents=True, exist_ok=True)
    (root / "mesh" / "nuc" / "M-01.md").write_text("one", encoding="utf-8")
    return root


def test_take_creates_a_listed_snapshot(tree):
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    assert stamp in [s["stamp"] for s in snapshot.listing(tree)]
    assert (tree / snapshot.DIR / stamp / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_a_snapshot_is_hardlinked_not_copied(tree):
    """The cost model the whole feature rests on: inodes, not bytes."""
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    a = (tree / "mesh" / "nuc" / "M-01.md").stat()
    b = (tree / snapshot.DIR / stamp / "mesh" / "nuc" / "M-01.md").stat()
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def test_an_atomic_rewrite_does_not_touch_the_snapshot(tree):
    """Hardlinks are only safe because every writer replaces the entry."""
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-01.md", b"two")
    assert (tree / snapshot.DIR / stamp / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_take_on_an_empty_tree_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "empty"))
    root = _io.root()
    assert snapshot.take(root, "2026-08-26T100000Z")
    assert snapshot.listing(root)[0]["files"] == 0


def test_revert_restores_the_snapshotted_content(tree):
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-01.md", b"two")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-02.md", b"new")

    snapshot.revert(tree, stamp, stamp="2026-08-26T110000Z")

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
    assert not (tree / "mesh" / "nuc" / "M-02.md").exists()


def test_revert_snapshots_the_current_state_first(tree):
    """A revert must itself be revertible — one wrong call cannot destroy state."""
    first = snapshot.take(tree, "2026-08-26T100000Z")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-01.md", b"two")

    snapshot.revert(tree, first, stamp="2026-08-26T110000Z")
    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"

    snapshot.revert(tree, "2026-08-26T110000Z", stamp="2026-08-26T120000Z")
    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "two"


def test_okf_is_not_reverted(tree):
    """okf/ is authored by hand and sync never writes it, so it is not this
    feature's to roll back — reverting it would destroy untouched work."""
    (tree / "okf").mkdir(parents=True, exist_ok=True)
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    _io.write_atomic(tree / "okf" / "O-01.md", b"authored after the snapshot")

    snapshot.revert(tree, stamp, stamp="2026-08-26T110000Z")

    assert (tree / "okf" / "O-01.md").read_text() == "authored after the snapshot"


def test_pruning_keeps_the_newest_n(tree, monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_SNAPSHOTS", "3")
    for i in range(6):
        snapshot.take(tree, f"2026-08-26T10000{i}Z")
    assert [s["stamp"] for s in snapshot.listing(tree)] == [
        "2026-08-26T100005Z", "2026-08-26T100004Z", "2026-08-26T100003Z"]


def test_an_unparsable_keep_falls_back_rather_than_raising(tree, monkeypatch):
    monkeypatch.setenv("AIFORGE_SYNC_SNAPSHOTS", "lots")
    assert snapshot.keep() == 10


def test_reverting_to_an_unknown_stamp_raises_and_changes_nothing(tree):
    with pytest.raises(FileNotFoundError):
        snapshot.revert(tree, "nope")
    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
    assert snapshot.listing(tree) == []


def test_a_snapshot_is_never_advertised(tree):
    """`.snapshots` is dotted, so `_io._hidden_below` already excludes it. This
    test is what stops somebody renaming it to something undotted and
    replicating every revert point to the whole fleet."""
    from aiforge_core.memory.sync import manifest

    (tree / "mesh" / "nuc" / "M-01.md").write_text(
        '---\ntype: knowledge\nid: "M-01"\norigin: "nuc"\nrev: 1\n'
        'updated_by: "nuc"\nderived: mesh\n---\n\nbody\n', encoding="utf-8")
    snapshot.take(tree, "2026-08-26T100000Z")

    rows = manifest.build()
    assert rows, "the fixture node itself must be advertised"
    assert all(snapshot.DIR not in e["path"] for e in rows)
