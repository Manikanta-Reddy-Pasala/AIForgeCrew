"""``okf/.trash/`` is the operator saying "this is noise". The mesh must agree.

``Path.glob("**/*.md")`` descends into dot-directories, so a trashed node was
advertised in the manifest, served over ``/blob``, re-planted on every peer and
folded into the distilled mesh — the exact opposite of what the delete meant.
``okf/.tomb/`` is dotted too and must keep working: it is passed in *as* the
scanned root, so only components below a root are refused.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path / "md"


def _node(directory, node_id: str, body: str = "trashed knowledge"):
    from aiforge_core.memory.okf import nodes

    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{node_id}.md"
    p.write_text(nodes.render_node(
        "learning", node_id,
        {"title": node_id, "scope": "global", "topic": "sync", "origin": "nuc",
         "rev": 1, "updated_by": "nuc"}, body), encoding="utf-8")
    return p


def _tomb(mem, key: str):
    p = mem / "okf" / ".tomb" / "nuc" / f"{key}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"origin": "nuc", "key": key, "rev": 9,
                             "updated_by": "nuc", "tomb": True}),
                 encoding="utf-8")
    return p


def test_a_trashed_node_is_not_advertised_but_a_tombstone_still_is(mem):
    from aiforge_core.memory.sync import manifest

    _node(mem / "okf" / ".trash", "L-09")
    _tomb(mem, "L-08")

    keys = {e.get("key") for e in manifest.build()}

    assert "L-09" not in keys
    assert "L-08" in keys


def test_a_trashed_node_cannot_be_served_over_blob(mem):
    from aiforge_core.memory.sync import _io, manifest

    trashed = _node(mem / "okf" / ".trash", "L-09")

    # /blob resolves a hash, and only what build() advertised is resolvable.
    assert manifest.path_for_hash(_io.sha256_file(trashed)) is None


def test_a_trashed_node_is_never_folded_into_the_mesh(mem, monkeypatch):
    """The leader distils over its inbox plus its own okf/. A trashed node in
    there becomes mesh-wide distilled knowledge, unrecoverably."""
    from aiforge_core.memory.okf import tiers

    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate",
                        lambda *a, **k: pytest.fail("trashed node was folded"))
    _node(mem / "okf" / ".trash", "L-09")

    # No peers → we are trivially the leader, so the fold really does run.
    assert tiers.distil_mesh()["skipped"] == "no-inputs"


def test_a_live_node_beside_the_trash_is_still_advertised(mem):
    """The exclusion is dot-directories, not "anything under okf/"."""
    from aiforge_core.memory.sync import manifest

    _node(mem / "okf" / "global" / "learnings", "L-10")
    _node(mem / "okf" / ".trash", "L-09")

    assert {e.get("key") for e in manifest.build()} == {"L-10"}
