"""The fold has to reach an agent, or the whole sync feature is inert.

Knowledge is pushed into the admin's ``peers/``, the admin folds it into
``mesh/`` and tier 2 folds that into ``view/`` — and none of it matters until a
retrieval path reads the result. The spec names exactly one shape for that:
agents read ``okf/`` plus ``view/``, and never the same fact twice. On a spoke,
which runs no tier 2, ``mesh/`` stands in for the view — never alongside it.
"""
from __future__ import annotations

import pytest

_FACT = "redis evictions need maxmemory-policy allkeys-lru"


@pytest.fixture
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


def _mesh_node(mem, body: str):
    from aiforge_core.memory.okf import nodes

    d = mem / "mesh" / "nuc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "M-sync-abc12345.md").write_text(
        nodes.render_node("learning", "M-sync-abc12345",
                          {"title": "M", "scope": "global", "derived": "mesh",
                           "origin": "nuc", "rev": 1, "updated_by": "nuc"},
                          body), encoding="utf-8")


def test_the_mesh_is_never_read_beside_the_view(mem):
    """``mesh/`` is an INPUT to the view, not a second retrieval source: reading
    both surfaces the same content raw *and* distilled."""
    from aiforge_core.memory.okf.retrieve import context_block

    _mesh_node(mem, "redis evictions raw mesh text")
    _view_node(mem, "V-sync-abc12345", _FACT)

    out = context_block(repo=None, query="redis evictions")

    assert _FACT in out
    assert "raw mesh text" not in out


def test_with_no_view_the_mesh_itself_reaches_the_agent(mem):
    """A spoke runs no tier 2 — the admin already did the distilling — so its
    ``view/`` stays empty. Returning nothing there would leave every spoke's
    agents on purely local memory while the fold sat unread on disk."""
    from aiforge_core.memory.okf.retrieve import context_block

    _mesh_node(mem, "redis evictions need maxmemory-policy allkeys-lru")

    out = context_block(repo=None, query="redis evictions")

    assert "maxmemory-policy allkeys-lru" in out


def test_a_failed_view_build_leaves_the_previous_view_intact(tmp_path, monkeypatch):
    """view/ is what agents read. Half old and half new is worse than stale."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "ms")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import _io, paths

    view = paths.view_dir()
    view.mkdir(parents=True, exist_ok=True)
    (view / "V-01.md").write_text("the good view", encoding="utf-8")

    mesh = paths.mesh_dir() / "ms"
    mesh.mkdir(parents=True, exist_ok=True)
    (mesh / "M-01.md").write_text(
        '---\ntype: knowledge\nid: "M-01"\norigin: "ms"\nrev: 1\n'
        'updated_by: "ms"\nderived: mesh\n---\n\nthe fold, in `loop.py`\n',
        encoding="utf-8")

    def _boom(**_kw):
        raise RuntimeError("the learner is down")

    monkeypatch.setattr(tiers, "_run_tier", _boom)
    result = tiers.build_view()

    assert result["ok"] is False
    assert (view / "V-01.md").read_text() == "the good view"
    assert not (view.parent / "view.tmp").exists()


def test_a_failed_view_build_does_not_advance_the_fingerprint(tmp_path, monkeypatch):
    """Otherwise the next cycle believes the view is current and never retries."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg2"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md2"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "ms")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    mesh = paths.mesh_dir() / "ms"
    mesh.mkdir(parents=True, exist_ok=True)
    (mesh / "M-01.md").write_text(
        '---\ntype: knowledge\nid: "M-01"\norigin: "ms"\nrev: 1\n'
        'updated_by: "ms"\nderived: mesh\n---\n\nthe fold, in `loop.py`\n',
        encoding="utf-8")

    calls = []

    def _boom(**_kw):
        calls.append(1)
        raise RuntimeError("the learner is down")

    monkeypatch.setattr(tiers, "_run_tier", _boom)
    tiers.build_view()
    tiers.build_view()

    assert len(calls) == 2, "a failed build must be retried, not marked current"
