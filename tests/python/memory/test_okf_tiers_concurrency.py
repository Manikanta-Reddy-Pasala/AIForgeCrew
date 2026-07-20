"""Two distributed-correctness defects a mesh-simulation review confirmed.

Both live in ``okf.tiers`` and both fail silently — knowledge that never travels
rather than a crash:

* FINDING 1 — tier-1 lost update. ``distil_mesh`` used to recompute its staleness
  fingerprint *after* the fold, so a node that landed in ``okf/`` or ``peers/``
  between reading the inputs and saving the stamp was recorded as already-folded
  and never reached any peer's ``view/`` until an unrelated file changed.
* FINDING 3 — substring suppression. ``_unrepresented`` tested ``claim not in
  <concatenated mesh text>``, so a local "use port 8080" was declared already
  represented by the mesh's "never use port 8080 for the gateway" — the negation
  swallowed its own affirmation, twice (tier-2 input selection and recall).

Each test fails on the pre-fix code and passes after.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path / "md"


def _plain(existing, new_content, *, role="learner", label=None, **kw):
    """Deterministic stand-in for the LLM merge: facts are the content lines."""
    return {"objective": "", "key_results": [],
            "facts": [ln.strip() for ln in (new_content or "").splitlines()
                      if ln.strip() and not ln.startswith("#")],
            "links": [], "learnings": []}


@pytest.fixture()
def merge(monkeypatch):
    monkeypatch.setattr("aiforge_core.runtime.work_notes.consolidate", _plain)


def _write_node(directory, node_id: str, body: str, *, topic: str = "sync",
                origin: str = "nuc", derived: str = ""):
    from aiforge_core.memory.okf import nodes

    meta = {"title": node_id, "scope": "global", "topic": topic,
            "origin": origin, "rev": 1, "updated_by": origin}
    if derived:
        meta["derived"] = derived
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"{node_id}.md"
    p.write_text(nodes.render_node("learning", node_id, meta, body),
                 encoding="utf-8")
    return p


def _learnings():
    from aiforge_core.memory.sync import paths

    return paths.okf_dir() / "global" / "learnings"


def _bodies(directory) -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(directory.rglob("*.md")))


# ── FINDING 1 — a mid-fold arrival must not be stamped as already-folded ────

def test_a_peer_node_arriving_during_the_fold_is_folded_next_cycle(mem, monkeypatch):
    """A ``peers/`` node landing between _load and _save_state used to be baked
    into the 'unchanged' stamp and never folded. It must instead leave the stamp
    stale so the next cycle re-folds and carries it."""
    import aiforge_core.runtime.work_notes as wn
    from aiforge_core.memory.okf import nodes, tiers
    from aiforge_core.memory.sync import paths

    monkeypatch.setattr(wn, "consolidate", _plain)
    _write_node(_learnings(), "L-01", "nuc fact one")
    tiers.distil_mesh()  # fold #1 — establishes the stamp

    # Arrange a peer node to land in peers/ DURING the next fold's merge, i.e.
    # after distil_mesh has already read its inputs.
    landed = {"done": False}

    def _mid(existing, new_content, *, role="learner", label=None, **kw):
        out = _plain(existing, new_content, role=role, label=label, **kw)
        if not landed["done"]:
            landed["done"] = True
            d = paths.peers_root() / "ms"
            d.mkdir(parents=True, exist_ok=True)
            (d / "L-09.md").write_text(nodes.render_node(
                "learning", "L-09",
                {"title": "L-09", "scope": "global", "topic": "sync",
                 "origin": "ms", "rev": 1, "updated_by": "ms"},
                "MS ARRIVAL DURING FOLD"), encoding="utf-8")
        return out

    monkeypatch.setattr(wn, "consolidate", _mid)
    _write_node(_learnings(), "L-02", "nuc fact two")  # make fold #2 run
    tiers.distil_mesh()  # fold #2 — the arrival lands mid-merge
    monkeypatch.setattr(wn, "consolidate", _plain)

    # The very next cycle must see the tree as changed and fold L-09 in — before
    # the fix it answered 'unchanged' and L-09 sat unfolded forever.
    out = tiers.distil_mesh()
    assert out.get("skipped") != "unchanged", out
    assert "MS ARRIVAL DURING FOLD" in _bodies(paths.mesh_dir())


def test_a_local_note_arriving_during_the_fold_is_folded_next_cycle(mem, monkeypatch):
    """The likelier case: an agent authors into ``okf/`` (a _tier1_dir) while the
    fold runs. Same defect, same fix."""
    import aiforge_core.runtime.work_notes as wn
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    monkeypatch.setattr(wn, "consolidate", _plain)
    _write_node(_learnings(), "L-01", "nuc fact one")
    tiers.distil_mesh()

    landed = {"done": False}

    def _mid(existing, new_content, *, role="learner", label=None, **kw):
        out = _plain(existing, new_content, role=role, label=label, **kw)
        if not landed["done"]:
            landed["done"] = True
            _write_node(_learnings(), "L-42", "AGENT AUTHORED THIS MID FOLD")
        return out

    monkeypatch.setattr(wn, "consolidate", _mid)
    _write_node(_learnings(), "L-02", "nuc fact two")
    tiers.distil_mesh()
    monkeypatch.setattr(wn, "consolidate", _plain)

    out = tiers.distil_mesh()
    assert out.get("skipped") != "unchanged", out
    assert "AGENT AUTHORED THIS MID FOLD" in _bodies(paths.mesh_dir())


def test_an_unchanged_tree_still_costs_no_fold(mem, merge):
    """The fix must not turn every change into a redundant fold: with the inputs
    and mesh both settled, the next cycle skips. (mesh/ is the fold's own output,
    so freezing only the *inputs* keeps this true.)"""
    from aiforge_core.memory.okf import tiers

    _write_node(_learnings(), "L-01", "nuc fact one")
    tiers.distil_mesh()

    assert tiers.distil_mesh() == {"ok": True, "skipped": "unchanged"}


# ── FINDING 3 — representation is whole-line, never substring ───────────────

def test_a_negation_does_not_suppress_its_own_affirmation():
    """The core defect, at unit level: "use port 8080" is a substring of "never
    use port 8080 for the gateway" but is NOT the same claim, so it must survive
    as unrepresented."""
    from aiforge_core.memory.okf import tiers

    mesh = [{"id": "M", "body": "never use port 8080 for the gateway"}]
    local = [{"id": "L", "body": "use port 8080"}]

    assert [n["id"] for n in tiers._unrepresented(local, mesh)] == ["L"]


def test_a_claim_the_mesh_truly_carries_is_still_dropped():
    """The dedupe half must still work: a whole-line match IS represented."""
    from aiforge_core.memory.okf import tiers

    mesh = [{"id": "M", "body": "the tunnel mtu is 1380."}]
    # same fact, only trailing punctuation differs — normalisation collapses it
    local = [{"id": "L", "body": "the tunnel mtu is 1380"}]

    assert tiers._unrepresented(local, mesh) == []


def test_a_headings_only_local_node_is_not_silently_dropped():
    """A node whose body is headings-only makes no claim, so the old ``any()``
    over an empty list dropped it. It states nothing the mesh could 'already
    carry', so it must be kept."""
    from aiforge_core.memory.okf import tiers

    mesh = [{"id": "M", "body": "some real fact"}]
    local = [{"id": "L", "body": "## Facts\n\n### A section"}]

    assert [n["id"] for n in tiers._unrepresented(local, mesh)] == ["L"]


def test_the_view_keeps_a_local_fact_a_mesh_negation_would_swallow(mem, merge):
    """End to end through tier 2: the affirmation reaches ``view/`` instead of
    being suppressed by the mesh's negation."""
    from aiforge_core.memory.okf import tiers
    from aiforge_core.memory.sync import paths

    _write_node(paths.mesh_dir() / "nuc", "M-sync",
                "never use port 8080 for the gateway", derived="mesh")
    _write_node(_learnings(), "L-77", "use port 8080")

    tiers.build_view()

    view = _bodies(paths.view_dir())
    assert any(ln.strip().lstrip("- ").strip() == "use port 8080"
               for ln in view.splitlines()), view


def test_recall_is_not_served_only_the_negation(mem):
    """The recall-side wiring (``tiers.unrepresented`` → retrieve._scoped_block):
    a local node whose claim is a substring of a view line must not be dropped,
    or the agent sees only the negation."""
    from aiforge_core.memory.okf import nodes, tiers
    from aiforge_core.memory.sync import paths

    d = paths.view_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "V-sync.md").write_text(nodes.render_node(
        "learning", "V-sync",
        {"title": "sync", "scope": "global", "topic": "sync", "derived": "view",
         "origin": "nuc", "rev": 1, "updated_by": "nuc"},
        "## Facts\n\n- never use port 8080 for the gateway"), encoding="utf-8")
    _write_node(_learnings(), "L-77", "use port 8080")

    local = tiers._usable(tiers._load((paths.okf_dir(),)))
    view = tiers.view_nodes()
    keep = tiers.unrepresented(local, view)

    assert [n["id"] for n in keep] == ["L-77"], keep
