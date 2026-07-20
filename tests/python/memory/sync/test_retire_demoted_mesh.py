"""Finding 7: a demoted leader's own mesh/<id>/ fold must not be immortal.

A fold is a class B node — advertised, replicated, keyed on its minting origin
(mesh/<origin>/). So after a handover the old leader's subtree otherwise rides
every future sync to every peer and every NEW peer forever, one dead subtree per
leadership change: ``_prune`` only touches the *current* leader's own dir, and
no other peer may delete a foreign mesh node (the next pull refetches it, and
forging a tombstone for another origin is what ``apply`` refuses).

The remedy is that the retiring owner tombstones its OWN fold through the
self-origin-guarded ``tombstone.mark_deleted``, and that tombstone propagates
the removal. A *dead* ex-leader cannot run this (that would need another peer to
forge its deletion), so this covers the alive-demotion handover — a smaller-id
peer joining — which is the common case.
"""
from __future__ import annotations

import time

import pytest


def _env(monkeypatch, tmp_path, peer_id: str):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", peer_id)


def _approve(*entries: tuple[str, int]) -> None:
    from aiforge_core.memory.sync import peers

    now = int(time.time())
    data = peers.load()
    data["peers"] = [{"id": pid, "urls": [f"http://{pid}:8799"],
                      "state": peers.STATE_APPROVED, "last_seen": now - ago}
                     for pid, ago in entries]
    peers.save(data)


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


def test_a_demoted_leader_tombstones_its_own_stale_fold(monkeypatch, tmp_path, folds):
    """``air`` leads and folds, then a smaller-id peer ``aa`` joins alive: air is
    demoted and must retract its own mesh subtree, leaving a tombstone."""
    _env(monkeypatch, tmp_path, "air")
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_okf("air", "L-01", "air knows a fact")
    tiers.run_after_sync()          # air is the lone leader → folds mesh/air/
    mesh_air = paths.mesh_dir() / "air"
    folded = list(mesh_air.glob("*.md"))
    assert folded, "the leader never wrote its own fold"
    key = folded[0].stem

    # A lexicographically-smaller peer appears, alive → air is no longer leader.
    _approve(("aa", 60))
    from aiforge_core.memory.sync import election
    assert election.leader() == "aa"
    assert election.effective_leader() == "aa"

    tiers.run_after_sync()          # air demoted → retires its own fold

    assert not list(mesh_air.glob("*.md")), "the demoted fold was not removed"
    tomb = paths.tomb_path("air", key)
    assert tomb.is_file(), "no tombstone was minted for the retracted fold"
    import json
    rec = json.loads(tomb.read_text(encoding="utf-8"))
    assert rec.get("tomb") is True and rec.get("origin") == "air"


def test_the_retirement_tombstone_removes_the_fold_on_another_peer(
        monkeypatch, tmp_path, folds):
    """The retraction must *propagate*: air's tombstone, applied on a peer still
    holding air's fold, removes it — otherwise the next pull refetches it."""
    from aiforge_core.memory.sync import apply, manifest, merge, paths

    # 1) air folds, is demoted, and retires — producing the tombstone.
    _env(monkeypatch, tmp_path, "air")
    from aiforge_core.memory.okf import tiers
    _write_okf("air", "L-01", "air knows a fact")
    tiers.run_after_sync()
    key = list((paths.mesh_dir() / "air").glob("*.md"))[0].stem
    _approve(("aa", 60))
    tiers.run_after_sync()

    manifest._CACHE.clear()
    tomb_entry = next(e for e in manifest.build()
                      if e.get("tomb") and e.get("key") == key)
    tomb_body = manifest.path_for_hash(tomb_entry["hash"]).read_bytes()

    # 2) a second peer 'ms' still holds air's fold at mesh/air/<key>.md.
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
