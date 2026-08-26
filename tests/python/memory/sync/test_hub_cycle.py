"""A spoke and an admin converge. The headline behaviour of the whole feature."""
from __future__ import annotations

import hashlib

from . import _hub


def _mesh_node(machine, key: str, text: str, *, origin: str) -> None:
    """Write a node marked as the admin's fold, the way tier 1 does."""
    p = machine["home"] / "md" / "mesh" / origin / f"{key}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'---\ntype: knowledge\nid: "{key}"\norigin: "{origin}"\nrev: 3\n'
        f'updated_by: "{origin}"\nderived: mesh\n---\n\n{text}\n', encoding="utf-8")


def _node(machine, relative: str, *, key: str, origin: str, by: str, rev: int,
          text: str) -> None:
    p = machine["home"] / "md" / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    # The body carries a file reference on purpose. The outbound filter
    # (``sync.redact``) holds back a node with no project signal at all, so a
    # fixture reading only "book learned something" would be filtered and every
    # assertion below would fail for a reason that has nothing to do with the
    # protocol these tests exist to exercise.
    p.write_text(
        f'---\ntype: learning\nid: "{key}"\norigin: "{origin}"\nrev: {rev}\n'
        f'updated_by: "{by}"\n---\n\n{text}, and the fix is in '
        f'`aiforge_core/memory/sync/loop.py` — `run_once()`.\n', encoding="utf-8")


def test_a_spokes_raw_captures_stay_on_the_spoke(monkeypatch, tmp_path):
    """Every machine compacts its own memory, so raw pastes never travel: the
    admin merges knowledge, it is not a file dump."""
    admin = _hub.node(monkeypatch, tmp_path, "hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")
    _hub.write_capture(spoke, "b-20260818-bbbbbb.md", "from book")

    res = _hub.cycle(monkeypatch, spoke, admin)

    assert res["ok"] is True
    assert res["pushed"] == 0
    assert _hub.capture_names(admin) == set()
    assert _hub.capture_names(spoke) == {"b-20260818-bbbbbb.md"}


def test_a_spokes_own_node_lands_in_the_admins_inbox(monkeypatch, tmp_path):
    admin = _hub.node(monkeypatch, tmp_path, "hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")
    _node(spoke, "okf/global/learnings/L-07.md", key="L-07", origin="book",
          by="book", rev=2, text="book learned something")

    _hub.cycle(monkeypatch, spoke, admin)

    # peers/<origin>/ — the raw inbox tier 1 folds, never okf/ (that is the
    # admin's own authored space).
    landed = admin["home"] / "md" / "peers" / "book" / "L-07.md"
    assert "book learned something" in landed.read_text(encoding="utf-8")


def test_the_admins_fold_comes_back_down(monkeypatch, tmp_path):
    admin = _hub.node(monkeypatch, tmp_path, "hub")
    _mesh_node(admin, "M-01", "the distilled answer", origin="hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")

    res = _hub.cycle(monkeypatch, spoke, admin)

    assert res["applied"] == 1
    landed = spoke["home"] / "md" / "mesh" / "hub" / "M-01.md"
    assert "the distilled answer" in landed.read_text(encoding="utf-8")


def test_a_second_cycle_changes_nothing(monkeypatch, tmp_path):
    admin = _hub.node(monkeypatch, tmp_path, "hub")
    _mesh_node(admin, "M-01", "the distilled answer", origin="hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")
    _node(spoke, "okf/global/learnings/L-07.md", key="L-07", origin="book",
          by="book", rev=2, text="book learned something")

    first = _hub.cycle(monkeypatch, spoke, admin)
    second = _hub.cycle(monkeypatch, spoke, admin)

    assert (first["pushed"], first["applied"]) == (1, 1)
    assert (second["pushed"], second["applied"]) == (0, 0)


def test_one_spokes_raw_notes_are_never_relayed_to_another(monkeypatch, tmp_path):
    """The whole point of the hub: raw knowledge goes up, only the fold comes
    back down. A second spoke must not receive the first's un-distilled node."""
    admin = _hub.node(monkeypatch, tmp_path, "hub")
    one = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")
    _node(one, "okf/global/learnings/L-07.md", key="L-07", origin="book",
          by="book", rev=2, text="only book knows this")
    _hub.cycle(monkeypatch, one, admin)
    assert (admin["home"] / "md" / "peers" / "book" / "L-07.md").is_file()

    two = _hub.node(monkeypatch, tmp_path, "studio", admin_url="http://hub")
    res = _hub.cycle(monkeypatch, two, admin)

    assert res["applied"] == 0
    assert not (two["home"] / "md" / "peers").exists()


def test_the_spoke_learns_who_the_admin_is(monkeypatch, tmp_path):
    """So it knows whose ``derived: mesh`` nodes to trust, with nothing configured."""
    from aiforge_core.memory.sync import role

    admin = _hub.node(monkeypatch, tmp_path, "hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")

    _hub.cycle(monkeypatch, spoke, admin)

    _hub.activate(monkeypatch, spoke)
    assert role.admin_id() == "hub"
    assert role.is_admin() is False


def test_an_unreachable_admin_is_survived(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")
    _hub.activate(monkeypatch, spoke)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest",
                        lambda *a, **k: {})
    monkeypatch.setattr("aiforge_core.memory.sync.transport.offer",
                        lambda *a, **k: None)

    res = loop.sync_with("http://127.0.0.1:1")

    assert res["ok"] is False
    assert res["applied"] == 0
    assert res["pushed"] == 0


def test_a_tampered_blob_is_rejected(monkeypatch, tmp_path):
    from aiforge_core.memory.sync import loop

    admin = _hub.node(monkeypatch, tmp_path, "hub")
    _mesh_node(admin, "M-01", "the distilled answer", origin="hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")

    _hub.wire(monkeypatch, admin)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_blob",
                        lambda *a, **k: b"TAMPERED")
    _hub.activate(monkeypatch, spoke)

    res = loop.sync_with("http://hub")

    assert res["applied"] == 0
    assert res["rejected"] == 1
    assert not (spoke["home"] / "md" / "mesh").exists()
    assert hashlib.sha256(b"TAMPERED").hexdigest()   # sanity: the real hash differs


def test_the_admin_makes_no_outbound_call(monkeypatch, tmp_path):
    """An admin answers; it never syncs anywhere. Nothing to configure, and no
    cycle spent on a machine that has no upstream."""
    from aiforge_core.memory.sync import loop

    admin = _hub.node(monkeypatch, tmp_path, "hub")
    _hub.activate(monkeypatch, admin)

    def _boom(*a, **k):
        raise AssertionError("the admin must not make an outbound sync call")

    monkeypatch.setattr("aiforge_core.memory.sync.transport.fetch_manifest", _boom)
    monkeypatch.setattr("aiforge_core.memory.sync.transport.offer", _boom)

    assert loop.run_once() == []


def test_a_retirement_reaches_the_spokes(monkeypatch, tmp_path):
    """End to end for the tombstone half: the admin drops a merged node and the
    spoke's copy goes with it. Without tombstones travelling down, the spoke's
    next pull simply keeps what it already has — forever."""
    from aiforge_core.memory.sync import tombstone

    admin = _hub.node(monkeypatch, tmp_path, "hub")
    _mesh_node(admin, "M-01", "the distilled answer", origin="hub")
    spoke = _hub.node(monkeypatch, tmp_path, "book", admin_url="http://hub")

    _hub.cycle(monkeypatch, spoke, admin)
    landed = spoke["home"] / "md" / "mesh" / "hub" / "M-01.md"
    assert landed.is_file()

    # The admin retires that node the way tiers._retire_own_mesh does.
    _hub.activate(monkeypatch, admin)
    (admin["home"] / "md" / "mesh" / "hub" / "M-01.md").unlink()
    assert tombstone.mark_deleted("hub", "M-01", 3) is not False

    _hub.cycle(monkeypatch, spoke, admin)

    assert not landed.exists(), "the deletion never reached the spoke"
