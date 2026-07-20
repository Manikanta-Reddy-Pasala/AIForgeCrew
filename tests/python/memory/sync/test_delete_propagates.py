"""Deletion needs a producer, not just a record format.

``tombstone.delete_node`` existed with no caller in the product: the two paths
that actually remove a node — ``okf.author``'s move to ``okf/.trash/`` and
``store.dedupe_nodes``' unlink — removed the file and said nothing, so the next
pull from any peer re-planted it. That is precisely the failure the tombstone
design names.
"""
from __future__ import annotations

import pytest

from .test_two_peer import _activate, _peer, _pull


def _learning(peer, node_id: str, body: str, *, origin: str = "nuc", rev: int = 1):
    from aiforge_core.memory.okf import nodes

    d = peer["home"] / "md" / "okf" / "global" / "learnings"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{node_id}.md"
    p.write_text(nodes.render_node(
        "learning", node_id,
        {"title": node_id, "scope": "global", "origin": origin, "rev": rev,
         "updated_by": origin}, body), encoding="utf-8")
    return p


def _decide_noise(monkeypatch, *node_ids: str):
    """Make the reclassifier call every given learning noise, with no LLM."""
    class _Dec:
        def __init__(self, nid):
            self.id, self.decision, self.repo = nid, "noise", ""

    class _Out:
        decisions = [_Dec(n) for n in node_ids]

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        lambda *a, **k: _Out())


def test_trashing_a_node_removes_it_from_a_second_peer(monkeypatch, tmp_path):
    from aiforge_core.memory.okf import author

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _learning(nuc, "L-01", "a transient test-session artifact")
    book = _peer(monkeypatch, tmp_path, "book")

    _pull(monkeypatch, book, nuc)
    landed = book["home"] / "md" / "peers" / "nuc" / "L-01.md"
    assert landed.is_file()          # the peer really is holding our node…

    _activate(monkeypatch, nuc)
    _decide_noise(monkeypatch, "L-01")
    assert author.reclassify_global_learnings(["Repo"])["deleted_to_trash"] == 1
    assert (nuc["home"] / "md" / "okf" / ".trash" / "L-01.md").is_file()

    _pull(monkeypatch, book, nuc)

    assert not landed.exists()       # …and the deletion reached it.


def test_dedupe_tombstones_the_losers_and_not_the_survivor(monkeypatch, tmp_path):
    from aiforge_core.memory.okf import store

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _activate(monkeypatch, nuc)
    fact = "redis evictions need maxmemory-policy allkeys-lru"
    store.save_node("learning", "L-01", {"scope": "global"}, fact)
    store.save_node("learning", "L-02", {"scope": "global"}, fact)

    assert store.dedupe_nodes()["removed"] == 1

    tombs = nuc["home"] / "md" / "okf" / ".tomb" / "nuc"
    assert (tombs / "L-02.json").is_file()      # the loser is announced…
    assert not (tombs / "L-01.json").exists()   # …the survivor keeps its identity.


def test_dedupe_never_speaks_for_another_peer(monkeypatch, tmp_path):
    """Only the minting peer may tombstone its identity — the outbound half of
    the refusal ``apply._accept_class_b`` already makes on the way in."""
    from aiforge_core.memory.okf import store

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _activate(monkeypatch, nuc)
    fact = "redis evictions need maxmemory-policy allkeys-lru"
    store.save_node("learning", "L-01", {"scope": "global"}, fact)
    _learning(nuc, "L-03", fact, origin="ms")

    assert store.dedupe_nodes()["removed"] == 1

    assert not (nuc["home"] / "md" / "okf" / ".tomb" / "ms" / "L-03.json").exists()


@pytest.mark.parametrize("origin", ["nuc", "ms"])
def test_mark_deleted_refuses_while_a_copy_survives(monkeypatch, tmp_path, origin):
    """Ids are per-scope counters, so one (origin, key) can name two files. A
    tombstone while one survives would delete it on every peer."""
    from aiforge_core.memory.sync import tombstone

    nuc = _peer(monkeypatch, tmp_path, "nuc")
    _activate(monkeypatch, nuc)
    _learning(nuc, "L-01", "still here", origin=origin)

    assert tombstone.mark_deleted(origin, "L-01", 1) is False
    assert not (nuc["home"] / "md" / "okf" / ".tomb" / origin).exists()
