"""Snapshots and revert.

Hardlinks, so a snapshot of a tree of markdown notes costs inodes and no bytes —
which is what makes "snapshot before every fold" affordable, and affordability
is the whole design. A snapshot somebody has to remember to take is one nobody
has.
"""
from __future__ import annotations

import shutil

import pytest

from aiforge_core.memory.sync import _io, snapshot


@pytest.fixture
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


# ── the operator routes ──────────────────────────────────────────────────

def _admin_api(monkeypatch, tmp_path):
    import importlib

    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    for k in ("AIFORGE_NEO4J_URI", "NEO4J_URI", "AIFORGE_API_TOKEN",
              "AIFORGE_BIND_HOST", "AIFORGE_ADMIN_URL", "AIFORGE_SYNC_GROUPS"):
        monkeypatch.delenv(k, raising=False)
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.api.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient
    # A real loopback peer address: /api/admin/* is loopback-only, and
    # TestClient's default host is "testclient", which the guard correctly
    # refuses. This populates scope["client"] exactly as a socket would, so the
    # production branch runs rather than being monkeypatched away.
    return TestClient(api.app, client=("127.0.0.1", 51000))


def test_the_routes_list_and_revert_a_groups_snapshots(tmp_path, monkeypatch):
    client = _admin_api(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import _io, group

    group.create("cellular")
    with group.scoped("cellular"):
        node = _io.root() / "mesh" / "nuc" / "M-01.md"
        node.parent.mkdir(parents=True, exist_ok=True)
        node.write_text("one", encoding="utf-8")
        stamp = snapshot.take(_io.root(), "2026-08-26T100000Z")
        _io.write_atomic(node, b"two")

    rows = client.get("/api/admin/memory/snapshots",
                      params={"group": "cellular"}).json()
    assert [s["stamp"] for s in rows["snapshots"]] == [stamp]

    r = client.post("/api/admin/memory/revert", params={"group": "cellular"},
                    json={"to": stamp})
    assert r.status_code == 200
    assert r.json()["previous_state"]          # the revert is itself revertible

    with group.scoped("cellular"):
        assert (_io.root() / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_reverting_an_unknown_group_is_404(tmp_path, monkeypatch):
    client = _admin_api(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import group

    group.create("cellular")
    r = client.post("/api/admin/memory/revert", params={"group": "typo"},
                    json={"to": "whatever"})
    assert r.status_code == 404


def test_reverting_to_an_unknown_stamp_is_404(tmp_path, monkeypatch):
    client = _admin_api(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import group

    group.create("cellular")
    r = client.post("/api/admin/memory/revert", params={"group": "cellular"},
                    json={"to": "nope"})
    assert r.status_code == 404


def test_the_ungrouped_tree_is_reachable_through_the_same_routes(tmp_path, monkeypatch):
    """An admin with no groups still has a tree worth reverting."""
    client = _admin_api(monkeypatch, tmp_path)
    from aiforge_core.memory.sync import _io

    node = _io.root() / "mesh" / "nuc" / "M-01.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("one", encoding="utf-8")
    snapshot.take(_io.root(), "2026-08-26T100000Z")

    r = client.get("/api/admin/memory/snapshots")
    assert r.status_code == 200
    assert [s["stamp"] for s in r.json()["snapshots"]] == ["2026-08-26T100000Z"]


# ── the client's own revert point ────────────────────────────────────────

def test_a_pull_that_changes_something_leaves_a_revert_point(tmp_path, monkeypatch):
    """A bad admin fold is one call to undo locally, without waiting for the
    admin to be fixed first."""
    from tests.python.memory.sync import _hub

    admin = _hub.node(monkeypatch, tmp_path, "hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")

    with _hub.serving(admin):
        d = _io.root() / "mesh" / "hub"
        d.mkdir(parents=True, exist_ok=True)
        (d / "M-01.md").write_text(
            '---\ntype: knowledge\nid: "M-01"\norigin: "hub"\nrev: 1\n'
            'updated_by: "hub"\nderived: mesh\n---\n\nthe fold, in `loop.py`\n',
            encoding="utf-8")

    _hub.activate(monkeypatch, spoke)
    _hub.run_once(monkeypatch, spoke, admin)

    assert snapshot.listing(_io.root()), "the pull left no revert point"


def test_an_idle_pull_does_not_churn_the_revert_points(tmp_path, monkeypatch):
    """A snapshot per empty cycle would push the useful ones out of the window
    within an hour."""
    from tests.python.memory.sync import _hub

    admin = _hub.node(monkeypatch, tmp_path, "hub2")
    spoke = _hub.node(monkeypatch, tmp_path, "book2", admin_url="http://hub2")

    _hub.activate(monkeypatch, spoke)
    _hub.run_once(monkeypatch, spoke, admin)
    _hub.run_once(monkeypatch, spoke, admin)

    assert snapshot.listing(_io.root()) == []


def test_reverting_to_the_oldest_snapshot_does_not_destroy_it(tree, monkeypatch):
    """The safety snapshot ``revert`` takes first must never prune the snapshot
    it is about to restore FROM.

    Found in review: ``take()`` prunes to ``keep()``, so reverting to the oldest
    snapshot deleted the source, and the restore then found nothing — after the
    live tree had already been removed. The whole received tree was lost, and
    "roll back as far as I can" is the commonest reason to revert at all.
    """
    monkeypatch.setenv("AIFORGE_SYNC_SNAPSHOTS", "2")
    oldest = snapshot.take(tree, "2026-08-26T100000Z")
    snapshot.take(tree, "2026-08-26T100001Z")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-01.md", b"a bad fold")

    snapshot.revert(tree, oldest, stamp="2026-08-26T110000Z")

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"


def test_reverting_with_only_one_snapshot_kept_still_restores(tree, monkeypatch):
    """The same bug at its sharpest: keep=1 meant the safety snapshot evicted
    the source before it was read."""
    monkeypatch.setenv("AIFORGE_SYNC_SNAPSHOTS", "1")
    only = snapshot.take(tree, "2026-08-26T100000Z")
    _io.write_atomic(tree / "mesh" / "nuc" / "M-01.md", b"a bad fold")

    snapshot.revert(tree, only, stamp="2026-08-26T110000Z")

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
    assert (tree / "mesh").is_dir(), "the live tree must never be left removed"


def test_a_revert_whose_source_vanishes_leaves_the_tree_alone(tree, monkeypatch):
    """Defence in depth: if the source is gone for any reason, refuse BEFORE
    removing the live tree. A half-applied revert is worse than none."""
    stamp = snapshot.take(tree, "2026-08-26T100000Z")
    real_take = snapshot.take

    def _take_then_sabotage(root, s="", *, protect=""):
        out = real_take(root, s, protect=protect)
        shutil.rmtree(tree / snapshot.DIR / stamp, ignore_errors=True)
        return out

    monkeypatch.setattr(snapshot, "take", _take_then_sabotage)
    with pytest.raises(FileNotFoundError):
        snapshot.revert(tree, stamp, stamp="2026-08-26T110000Z")

    assert (tree / "mesh" / "nuc" / "M-01.md").read_text() == "one"
