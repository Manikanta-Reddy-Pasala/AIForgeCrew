"""md_store brief tidy: no kind-named junk briefs, near-typo topics cluster,
tidy_briefs folds kind briefs into global + is a no-op under dry_run."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import aiforge_core.memory.md_store as md
    return importlib.reload(md)


def _write_brief(md, slug: str, facts: list[str]) -> None:
    from aiforge_core.memory.md_store._base import brief_path
    from aiforge_core.memory.md_store._render import _render_brief
    p = brief_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_render_brief(slug, facts=facts), encoding="utf-8")


def _brief_stems(md) -> set[str]:
    from aiforge_core.memory.md_store._base import iter_briefs
    return {p.stem[len("compacted-"):] for p in iter_briefs()}


def test_untopiced_note_makes_no_kind_brief(mem):
    """A note the topic labeller can't theme (no topic, no model in tests) must
    NOT spawn a compacted-<kind>.md file in topic-mode compaction."""
    mem.capture("learning", "Prefer async IO across the board",
                repo=None, topic=None, classify=False)
    mem.compact(group_by="topic", min_group=1, summarize=False,
                archive_sources=False)
    stems = _brief_stems(mem)
    assert "learning" not in stems          # no kind-named junk brief
    assert "note" not in stems


def test_topic_clusters_catch_near_typo(mem):
    from aiforge_core.memory.md_store._graph._reconcile import _topic_clusters
    clusters = _topic_clusters(["gps", "gpst", "auth"])
    flat = {k for c in clusters for k in c}
    assert {"gps", "gpst"} <= flat          # near-typo pair clustered
    assert "auth" not in flat               # distinct slug left alone


def test_tidy_folds_kind_brief_into_shared(mem):
    _write_brief(mem, "shared", ["global rule one"])
    _write_brief(mem, "learning", ["a stray learning fact"])
    assert "learning" in _brief_stems(mem)
    res = mem.tidy_briefs()
    assert res["ok"]
    assert res["folded_kind"] >= 1
    stems = _brief_stems(mem)
    assert "learning" not in stems          # kind brief deleted
    # its fact now lives in the global shared brief
    from aiforge_core.memory.md_store._base import brief_path
    shared = brief_path("shared").read_text(encoding="utf-8")
    assert "a stray learning fact" in shared


def test_tidy_dry_run_touches_nothing(mem):
    _write_brief(mem, "shared", ["g1"])
    _write_brief(mem, "user-comment", ["stray comment"])
    before = _brief_stems(mem)
    res = mem.tidy_briefs(dry_run=True)
    assert res["dry_run"]
    assert res["folded_kind"] >= 1
    assert _brief_stems(mem) == before      # nothing deleted/written
