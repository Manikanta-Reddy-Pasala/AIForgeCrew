"""Two-tier knowledge compaction: the leader's mesh fold, the local view.

The two loop-breaks are what these mostly defend. Tier 1 refuses to re-fold
content that is already distilled, and tier-2 output is never advertised — undo
either and knowledge amplifies a little on every round, which reads fine for
days before the notes start drifting.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path / "md"


@pytest.fixture
def folds(monkeypatch):
    """Record every merge, and keep the LLM out of it. Returns the call list."""
    calls: list[str] = []

    def _fake(existing, new_content, *, role="learner", label=None, **kw):
        calls.append(str(label or ""))
        return {"objective": "", "key_results": [],
                "facts": [ln.strip() for ln in (new_content or "").splitlines()
                          if ln.strip() and not ln.startswith("#")],
                "links": [], "learnings": []}

    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate", _fake)
    return calls


def _write_node(directory, node_id: str, body: str, *, topic: str = "sync",
                origin: str = "nuc", derived: str = "", ntype: str = "learning"):
    from aiforge_core.memory.okf import nodes

    meta = {"title": node_id, "scope": "global", "topic": topic,
            "origin": origin, "rev": 1, "updated_by": origin}
    if derived:
        meta["derived"] = derived
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{node_id}.md"
    p.write_text(nodes.render_node(ntype, node_id, meta, body), encoding="utf-8")
    return p


def _spoke_of(monkeypatch, admin_id: str) -> None:
    """Make this machine a spoke of ``admin_id``: it stops folding, and that
    machine's ``derived: mesh`` nodes become the ones it trusts."""
    monkeypatch.setenv("AIFORGE_ADMIN_URL", f"http://{admin_id}:8799")
    monkeypatch.setenv("AIFORGE_ADMIN_ID", admin_id)


def _read(path):
    from aiforge_core.memory.okf import nodes

    return nodes.parse_node(path.read_text(encoding="utf-8"))


def _node(directory, prefix: str):
    """The one compacted node under ``directory``, found rather than spelled.

    Two reasons the literal filename is gone: a node id now carries a digest of
    its raw group (distinct groups used to alias onto one file), and the leader
    writes its fold into its own ``mesh/<origin>/`` subtree.
    """
    found = sorted(p for p in directory.rglob(f"{prefix}-*.md"))
    assert len(found) == 1, found
    return found[0]


# ── tier 1 ────────────────────────────────────────────────────────────────

def test_the_leader_folds_every_inbox_and_its_own_okf_into_the_mesh(mem, folds):
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(mem / "peers" / "air", "L-01", "air knows the tunnel mtu",
                origin="air")
    _write_node(mem / "peers" / "ms", "L-02", "ms knows the retry backoff",
                origin="ms")
    _write_node(paths.okf_dir() / "global" / "learnings", "L-03",
                "nuc knows the lock timeout")

    out = tiers.distil_mesh()

    assert out["ok"]
    assert out["inputs"] == 3
    assert out["groups"] == 1
    node = _read(_node(paths.mesh_dir(), "M"))
    assert node["meta"]["derived"] == "mesh"
    for claim in ("tunnel mtu", "retry backoff", "lock timeout"):
        assert claim in node["body"]


def test_a_spoke_leaves_the_mesh_alone(mem, folds, monkeypatch):
    """The admin folds for everybody; a spoke spends no tokens on it."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _spoke_of(monkeypatch, "air")
    _write_node(mem / "peers" / "air", "L-01", "air knowledge", origin="air")

    out = tiers.distil_mesh()

    assert out["skipped"] == "not-admin"
    assert out["admin"] == "air"
    assert not list(paths.mesh_dir().rglob("*.md"))
    assert folds == []


def test_mesh_content_arriving_in_the_inbox_is_never_refolded(mem, folds):
    """Anti-amplification. A peer republishing the mesh must not feed tier 1."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(mem / "peers" / "air", "L-01", "authored by air", origin="air")
    _write_node(mem / "peers" / "air", "M-sync", "already distilled",
                origin="air", derived="mesh")

    out = tiers.distil_mesh()

    assert out["inputs"] == 1
    body = _read(_node(paths.mesh_dir(), "M"))["body"]
    assert "authored by air" in body
    assert "already distilled" not in body


def test_an_unchanged_inbox_costs_no_fold(mem, folds):
    from aiforge_core.memory.okf import tiers

    _write_node(mem / "peers" / "air", "L-01", "air knowledge", origin="air")
    tiers.distil_mesh()
    folds.clear()

    assert tiers.distil_mesh() == {"ok": True, "skipped": "unchanged"}
    assert folds == []


def test_new_knowledge_reopens_the_fold(mem, folds):
    """The skip is staleness, not a one-shot latch."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(mem / "peers" / "air", "L-01", "air knowledge", origin="air")
    tiers.distil_mesh()
    _write_node(mem / "peers" / "ms", "L-02", "later knowledge", origin="ms")

    assert tiers.distil_mesh()["inputs"] == 2
    assert "later knowledge" in _read(_node(paths.mesh_dir(), "M"))["body"]


# ── tier 2 ────────────────────────────────────────────────────────────────

def test_the_view_is_built_from_the_mesh_and_our_own_okf(mem, folds):
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    _write_node(paths.okf_dir() / "global" / "learnings", "L-03",
                "locally we override it to 1280")

    out = tiers.build_view()

    assert out["ok"]
    assert out["inputs"] == 2
    body = _read(_node(paths.view_dir(), "V"))["body"]
    assert "mtu 1380" in body
    assert "override it to 1280" in body


def test_an_unchanged_mesh_costs_no_fold(mem, folds):
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    tiers.build_view()
    folds.clear()

    assert tiers.build_view() == {"ok": True, "skipped": "unchanged"}
    assert folds == []


def test_a_local_note_reaches_the_view_without_waiting_for_a_new_mesh(mem, folds):
    """Own knowledge is a tier-2 input, so it must not be gated on the leader:
    keying only on the mesh hid a note here until the next publish, or forever
    while the leader was down."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    tiers.build_view()
    folds.clear()

    _write_node(paths.okf_dir() / "global" / "learnings", "L-03",
                "locally we override it to 1280")
    out = tiers.build_view()

    assert out["ok"]
    assert folds
    assert "override it to 1280" in _read(_node(paths.view_dir(), "V"))["body"]


def test_an_unchanged_okf_and_mesh_cost_no_merge(mem, folds, monkeypatch):
    """Widening what counts as a change must not make every cycle rebuild:
    authoring nothing still costs a directory walk and no LLM call."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    _write_node(paths.okf_dir() / "global" / "learnings", "L-03", "and 1280 here")
    tiers.build_view()

    def _never(*a, **kw):
        raise AssertionError("consolidate called on an unchanged tree")

    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate", _never)

    assert tiers.build_view() == {"ok": True, "skipped": "unchanged"}


def test_a_mesh_node_received_into_the_inbox_is_still_recognised(mem, folds,
                                                                 monkeypatch):
    """Tolerance kept on purpose: a build older than the mesh routing filed its
    copy under peers/, and so did every node received before it.

    Read through ``view_nodes`` rather than by building a view: tier 2 is
    admin-only now, and on a spoke the mesh IS the view (see ``view_nodes``).
    A mesh marker is only honoured from the admin (see the hostile-peer test).
    """
    from aiforge_core.memory.okf import tiers

    _spoke_of(monkeypatch, "air")
    _write_node(mem / "peers" / "air", "M-sync", "the mesh says mtu 1380",
                origin="air", derived="mesh")

    bodies = [n["body"] for n in tiers.view_nodes()]
    assert len(bodies) == 1
    assert "mtu 1380" in bodies[0]


def test_a_corrupt_mesh_leaves_a_good_view_standing(mem, folds):
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    tiers.build_view()
    view = _node(paths.view_dir(), "V")
    good = view.read_text(encoding="utf-8")

    (paths.mesh_dir() / "M-sync.md").write_text("", encoding="utf-8")
    out = tiers.build_view()

    assert out["skipped"] == "no-mesh"
    assert view.read_text(encoding="utf-8") == good


def test_the_view_is_never_advertised(mem, folds):
    """The loop-break. A synced view would return as mesh and re-merge forever."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import manifest, paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    tiers.build_view()

    assert list(paths.view_dir().rglob("*.md"))          # there IS a view
    assert not [e for e in manifest.build() if e["path"].startswith("view/")]


def test_the_view_is_rebuilt_rather_than_merged_into(mem, folds):
    """Safe to delete at any moment: the next build reproduces it exactly."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")
    tiers.build_view()
    first = _read(_node(paths.view_dir(), "V"))["body"]

    _node(paths.view_dir(), "V").unlink()
    tiers._save_state("view", [])          # forget the stamp, not the inputs
    tiers.build_view()

    assert _read(_node(paths.view_dir(), "V"))["body"] == first


# ── end to end ────────────────────────────────────────────────────────────

def test_a_lone_machine_runs_both_tiers_over_its_own_knowledge(mem, folds):
    """No peers: trivially the leader, empty inbox, a view of its own okf/."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.okf_dir() / "global" / "learnings", "L-01",
                "the daemon restarts under systemd")

    out = tiers.run_after_sync()

    assert out["mesh"]["ok"]
    assert out["view"]["ok"]
    assert "systemd" in _read(_node(paths.mesh_dir(), "M"))["body"]
    assert "systemd" in _read(_node(paths.view_dir(), "V"))["body"]


def test_an_empty_tree_folds_nothing_and_deletes_nothing(mem, folds):
    """A momentarily empty read must not answer by wiping everyone's mesh."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir(), "M-sync", "the mesh says mtu 1380",
                derived="mesh")

    assert tiers.distil_mesh()["skipped"] == "no-inputs"
    assert (paths.mesh_dir() / "M-sync.md").exists()
    assert folds == []


def test_both_tiers_work_with_no_model_reachable(mem):
    """No ``folds`` fixture: the real consolidate, on its deterministic path."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.okf_dir() / "global" / "learnings", "L-01",
                "the tunnel mtu is 1380")

    out = tiers.run_after_sync()

    assert out["mesh"]["ok"]
    assert out["view"]["ok"]
    assert "1380" in _read(_node(paths.view_dir(), "V"))["body"]


def test_a_failing_tier_is_reported_not_raised(mem, monkeypatch):
    """Compaction is upkeep: it may cost a cycle, never the daemon."""
    from aiforge_core.memory.okf import tiers

    def _boom(**kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tiers, "distil_mesh", _boom)
    out = tiers.run_after_sync()

    assert out["mesh"] == {"ok": False, "error": "disk on fire"}
    assert out["view"]["ok"]


def test_the_sync_cycle_runs_both_tiers_after_the_pass(mem, monkeypatch):
    """One moving part: no second schedule, no extra thread."""
    import contextlib

    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import loop

    class _Stop(BaseException):
        """Not an ``Exception``: ``run_forever`` deliberately outlives those."""

    order: list[str] = []
    monkeypatch.setattr(loop, "run_once", lambda: order.append("sync"))

    def _tiers(**kw):
        order.append("compact")
        raise _Stop

    monkeypatch.setattr(tiers, "run_after_sync", _tiers)
    with contextlib.suppress(_Stop):
        # Any positive interval: _tiers raises before the sleep is reached.
        # Zero is refused now — it made the loop spin without throttling.
        loop.run_forever(interval=1)

    assert order == ["sync", "compact"]
