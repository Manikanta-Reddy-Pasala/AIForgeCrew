"""Finding 2: a reachable-but-non-distilling leader must not starve the mesh.

The election proves the elected peer answers /manifest; it proves nothing about
whether that peer runs the fold. ``tiers.run_after_sync`` (the folder) and the
API app that serves /manifest are separate entry points, so a peer running only
``aiforge-api`` is a perfect *passive* leader: it stays elected, every follower
defers to it, and every view stays empty forever while every log line reads
``ok: True``.

These pin the fallback: after ``FALLBACK_AFTER`` silent cycles the next
candidate takes over — deterministically, so the mesh still has exactly one
folder — and a healthy leader keeps its followers deferring as before.
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
    """Keep the LLM out of the fold: facts = non-heading content lines."""
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


def test_a_passive_leader_is_taken_over_by_the_next_candidate(monkeypatch, tmp_path):
    """We are ``ms``; ``air`` leads but never produces a ``mesh/air/`` fold.

    For the grace window we still defer — the leader's first fold might just be
    in flight. After ``FALLBACK_AFTER`` silent cycles the leader is proven
    passive and WE, the next candidate, take over.
    """
    _env(monkeypatch, tmp_path, "ms")
    _approve(("air", 60), ("nuc", 60))
    from aiforge_core.memory.sync import election

    assert election.leader() == "air"
    assert election.is_leader() is False

    # No mesh/air/ exists, so every cycle counts as silent. may_distil advances
    # the timer once per call; the grace window still defers.
    for _ in range(election.FALLBACK_AFTER - 1):
        assert election.may_distil() is False
        assert election.effective_leader() == "air"

    # The leader is now provably passive: we are next in line, so we fold.
    assert election.may_distil() is True
    assert election.effective_leader() == "ms"


def test_a_third_peer_defers_to_the_fallback_not_itself(monkeypatch, tmp_path):
    """``nuc`` sees the same passive ``air`` but is NOT next in line — it must
    defer to ``ms`` rather than fold too, so the mesh keeps ONE folder."""
    _env(monkeypatch, tmp_path, "nuc")
    _approve(("air", 60), ("ms", 60))
    from aiforge_core.memory.sync import election

    for _ in range(election.FALLBACK_AFTER + 1):
        election.may_distil()
    # air is passive, but the successor is ms, not us.
    assert election.effective_leader() == "ms"
    assert election.may_distil() is False


def test_a_folding_leader_is_never_pre_empted(monkeypatch, tmp_path, folds):
    """The common case is untouched: once the leader's fold is visible here the
    timer resets, and a follower defers to it indefinitely."""
    _env(monkeypatch, tmp_path, "ms")
    _approve(("air", 60), ("nuc", 60))
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    # The leader HAS folded, and it replicated to us: mesh/air/ holds a node.
    d = paths.mesh_dir() / "air"
    d.mkdir(parents=True, exist_ok=True)
    from aiforge_core.memory.okf import nodes
    meta = {"title": "M-x", "scope": "global", "topic": "sync", "origin": "air",
            "rev": 1, "updated_by": "air", "derived": tiers.MESH}
    (d / "M-x.md").write_text(nodes.render_node("learning", "M-x", meta, "fact one"),
                              encoding="utf-8")

    from aiforge_core.memory.sync import election
    assert tiers.leader_has_mesh_output("air") is True
    # Well past the fallback window — a live fold must reset the timer every time.
    for _ in range(election.FALLBACK_AFTER + 3):
        assert election.may_distil() is False
    assert election.effective_leader() == "air"


def test_a_follower_of_a_passive_leader_eventually_builds_a_non_empty_view(
        monkeypatch, tmp_path, folds):
    """End to end (the E5 repro, single follower): ``ms`` authors knowledge, its
    elected leader ``air`` never folds, and after the grace window ``ms`` folds
    locally and its view — the only thing retrieval surfaces — is non-empty."""
    _env(monkeypatch, tmp_path, "ms")
    _approve(("air", 60), ("nuc", 60))
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import election, paths

    _write_okf("ms", "L-01", "ms knows a fact")

    # air never produces mesh/air/. Run the whole post-sync pass repeatedly.
    for _ in range(election.FALLBACK_AFTER + 2):
        tiers.run_after_sync()

    own_mesh = list((paths.mesh_dir() / "ms").glob("*.md"))
    view = list(paths.view_dir().rglob("*.md"))
    assert own_mesh, "the fallback folder never wrote its own mesh subtree"
    assert view, "the view stayed empty despite a passive leader"
    body = "\n".join(p.read_text(encoding="utf-8") for p in view)
    assert "ms knows a fact" in body
