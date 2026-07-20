"""``view/`` has to reach an agent, or the whole peer-to-peer feature is inert.

Knowledge replicates into ``peers/``, the leader folds it into ``mesh/`` and
tier 2 folds that into ``view/`` — and none of it matters until a retrieval path
reads the result. The spec names exactly one shape for that: agents read
``okf/`` plus ``view/``, and never the same fact twice.
"""
from __future__ import annotations

import pytest

_FACT = "redis evictions need maxmemory-policy allkeys-lru"


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path / "md"


def _view_node(mem, node_id: str, body: str):
    from aiforge_core.memory.okf import nodes

    directory = mem / "view"
    directory.mkdir(parents=True, exist_ok=True)
    meta = {"title": node_id, "scope": "global", "topic": "sync",
            "derived": "view", "origin": "nuc", "rev": 1, "updated_by": "nuc"}
    p = directory / f"{node_id}.md"
    p.write_text(nodes.render_node("learning", node_id, meta, body),
                 encoding="utf-8")
    return p


def test_recall_surfaces_a_fact_that_exists_only_in_the_view(mem):
    """The fold is the only place this fact lives. An agent must still get it."""
    from aiforge_core.memory.okf.retrieve import context_block

    _view_node(mem, "V-sync-abc12345", _FACT)

    out = context_block(repo=None, query="redis evictions")

    assert _FACT in out


def test_a_fact_held_both_locally_and_in_the_view_is_rendered_once(mem):
    """Tier 1 folded our own okf/ in, so the view restates what we authored.
    Rendering both would spend the prompt budget saying one thing twice."""
    from aiforge_core.memory.okf import store
    from aiforge_core.memory.okf.retrieve import context_block

    # No title: the global-rules line then renders the body itself, so a double
    # surface would be literally the same text twice.
    store.save_node("learning", "L-01", {"scope": "global"}, _FACT)
    _view_node(mem, "V-sync-abc12345", _FACT)

    out = context_block(repo=None, query="redis evictions")

    assert _FACT in out          # …reaches the agent…
    assert out.count(_FACT) == 1  # …and does so once, not once per tier.


def test_a_local_note_the_view_does_not_carry_still_surfaces(mem):
    """The de-duplication drops what the fold restates, not local knowledge the
    fold never saw — otherwise a note authored since the last mesh disappears."""
    from aiforge_core.memory.okf import store
    from aiforge_core.memory.okf.retrieve import context_block

    store.save_node("learning", "L-02", {"scope": "global"},
                    "redis evictions also need a keyspace notification channel")
    _view_node(mem, "V-sync-abc12345", _FACT)

    out = context_block(repo=None, query="redis evictions")

    assert "keyspace notification channel" in out
    assert _FACT in out


def test_the_view_is_read_but_the_mesh_inbox_is_not(mem):
    """``mesh/`` is an input to the view, not a second retrieval source: reading
    both surfaces the same content raw *and* distilled."""
    from aiforge_core.memory.okf import nodes
    from aiforge_core.memory.okf.retrieve import context_block

    d = mem / "mesh" / "nuc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "M-sync-abc12345.md").write_text(
        nodes.render_node("learning", "M-sync-abc12345",
                          {"title": "M", "scope": "global", "derived": "mesh",
                           "origin": "nuc", "rev": 1, "updated_by": "nuc"},
                          "redis evictions raw mesh text"), encoding="utf-8")

    out = context_block(repo=None, query="redis evictions")

    assert "raw mesh text" not in out
