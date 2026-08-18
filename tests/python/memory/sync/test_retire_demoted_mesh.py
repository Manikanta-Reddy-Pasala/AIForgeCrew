"""A machine that stops being the admin must not leave an immortal fold.

A fold is a class B node — advertised, replicated, keyed on its minting origin
(mesh/<origin>/). So after the role moves, the old admin's subtree otherwise
rides every future sync to every spoke and every NEW spoke forever, one dead
subtree per role change: ``_prune`` only touches the *current* fold's own dir,
and nobody else may delete a foreign mesh node (the next pull refetches it, and
forging a tombstone for another origin is what ``apply`` refuses).

The remedy is that the retiring owner tombstones its OWN fold through the
self-origin-guarded ``tombstone.mark_deleted``, and that tombstone propagates
the removal. A machine switched off at the moment it is demoted cannot run this
(that would need somebody else to forge its deletion), so this covers the
alive-demotion case — an operator pointing the box at a new admin.
"""
from __future__ import annotations

import pytest


def _env(monkeypatch, tmp_path, peer_id: str, admin: str = ""):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    if admin:
        monkeypatch.setenv("AIFORGE_ADMIN_URL", admin)
    else:
        monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)


@pytest.fixture()
def folds(monkeypatch):
    def _fake(existing, new_content, *, role="learner", label=None, **kw):
        return {"objective": "", "key_results": [],
                "facts": [ln.strip() for ln in (new_content or "").splitlines()
                          if ln.strip() and not ln.startswith("#")],
                "links": [], "learnings": []}

    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate", _fake)


def _write_okf(peer_id: str, node_id: str, body: str) -> None:
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.sync import paths

    d = paths.okf_dir() / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"title": node_id, "scope": "global", "topic": "sync",
            "origin": peer_id, "rev": 1, "updated_by": peer_id}
    (d / f"{node_id}.md").write_text(
        nodes.render_node("learning", node_id, meta, body), encoding="utf-8")


def test_a_demoted_admin_tombstones_its_own_stale_fold(monkeypatch, tmp_path, folds):
    """``air`` is the admin and folds, then the operator points it at a new
    admin: air must retract its own mesh subtree, leaving a tombstone."""
    _env(monkeypatch, tmp_path, "air")
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_okf("air", "L-01", "air knows a fact")
    tiers.run_after_sync()          # air is the admin → folds mesh/air/
    mesh_air = paths.mesh_dir() / "air"
    folded = list(mesh_air.glob("*.md"))
    assert folded, "the admin never wrote its own fold"
    key = folded[0].stem

    # The operator names a different admin, and that machine answers, so its id
    # is known — retirement needs a successor, not just the loss of the role.
    monkeypatch.setenv("AIFORGE_ADMIN_URL", "http://aa:8799")
    monkeypatch.setenv("AIFORGE_ADMIN_ID", "aa")
    from aiforge_core.memory.sync import role
    assert role.is_admin() is False

    tiers.run_after_sync()          # air demoted → retires its own fold

    assert not list(mesh_air.glob("*.md")), "the demoted fold was not removed"
    tomb = paths.tomb_path("air", key)
    assert tomb.is_file(), "no tombstone was minted for the retracted fold"
    import json
    rec = json.loads(tomb.read_text(encoding="utf-8"))
    assert rec.get("tomb") is True and rec.get("origin") == "air"


def test_the_retirement_tombstone_removes_the_fold_elsewhere(
        monkeypatch, tmp_path, folds):
    """The retraction must *propagate*: air's tombstone, applied on a machine
    still holding air's fold, removes it — otherwise the next pull refetches
    it."""
    from aiforge_core.memory.sync import apply, manifest, paths

    # 1) air folds, is demoted, and retires — producing the tombstone.
    _env(monkeypatch, tmp_path, "air")
    from aiforge_core.memory.okf import tiers
    _write_okf("air", "L-01", "air knows a fact")
    tiers.run_after_sync()
    key = list((paths.mesh_dir() / "air").glob("*.md"))[0].stem
    monkeypatch.setenv("AIFORGE_ADMIN_URL", "http://aa:8799")
    monkeypatch.setenv("AIFORGE_ADMIN_ID", "aa")
    tiers.run_after_sync()

    manifest._CACHE.clear()
    tomb_entry = next(e for e in manifest.build()
                      if e.get("tomb") and e.get("key") == key)
    tomb_body = manifest.path_for_hash(tomb_entry["hash"]).read_bytes()

    # 2) a second machine 'ms' still holds air's fold at mesh/air/<key>.md.
    _env(monkeypatch, tmp_path / "ms", "ms")
    from aiforge_core.memory.okf import nodes
    held = paths.mesh_dir() / "air"
    held.mkdir(parents=True, exist_ok=True)
    meta = {"title": key, "scope": "global", "topic": "sync", "origin": "air",
            "rev": 1, "updated_by": "air", "derived": tiers.MESH}
    (held / f"{key}.md").write_text(
        nodes.render_node("learning", key, meta, "air knows a fact"),
        encoding="utf-8")
    assert (held / f"{key}.md").is_file()

    # 3) ms applies air's tombstone (served BY air) → its copy is removed.
    ok = apply.apply_blob(tomb_entry, tomb_body, peer_id="air")
    assert ok is True
    assert not (held / f"{key}.md").is_file(), "tombstone did not remove the fold"


def test_a_role_lost_by_accident_keeps_the_fold(monkeypatch, tmp_path, folds):
    """The failure this guard exists for: the shipped service unit restarts
    run.sh WITHOUT --admin, so the admin comes back as a plain machine. Deleting
    the fleet's merged knowledge because of a missing flag is unrecoverable —
    the tombstones reach every spoke on its next pull — so retirement waits
    until another machine is actually known to be folding."""
    _env(monkeypatch, tmp_path, "air")
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_okf("air", "L-01", "air knows a fact")
    tiers.run_after_sync()                      # air is the admin → folds
    mesh_air = paths.mesh_dir() / "air"
    assert list(mesh_air.glob("*.md"))

    # Restarted as a spoke, with no admin reached yet: no successor is known.
    monkeypatch.setenv("AIFORGE_ROLE", "spoke")

    out = tiers.run_after_sync()

    assert out["retire"] == {"retired": 0, "skipped": "no-successor"}
    assert list(mesh_air.glob("*.md")), "the fold was deleted with no successor"
    assert not list((paths.tomb_dir()).rglob("*.json")), "and nothing was tombstoned"
