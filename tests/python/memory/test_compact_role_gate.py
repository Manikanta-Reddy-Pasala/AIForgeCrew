"""Local compaction is local, on every machine — admin and spoke alike.

Turning captures into briefs is work on one machine's own files, so nothing
about the hub gates it: only the CROSS-machine merge is admin-only (see
``test_okf_tiers``). What compact() does still ask is "which captures does a
brief already cover?", and that fails CLOSED — an archived capture no brief
covers is unrecoverable.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    monkeypatch.delenv("AIFORGE_ROLE", raising=False)
    return tmp_path


@pytest.fixture
def spoke(mem, monkeypatch):
    """This machine is a spoke: an admin is named. Compaction is unaffected."""
    monkeypatch.setenv("AIFORGE_ADMIN_URL", "http://10.0.0.9:8799")
    return mem


def _capture(tmp_path, stem: str, *, repo: str = "notes") -> None:
    from aiforge_core.memory import md_store

    p = md_store.captures_dir() / f"{stem}.md"
    p.write_text(f"---\ntitle: {stem}\nkind: note\ntags: \nsource: manual\n"
                 f"repo: {repo}\ntopic: \ncreated: 2026-07-19T00:00:00Z\n---\n\n"
                 f"body of {stem}\n", encoding="utf-8")


def _brief(tmp_path, key: str, sources: list[str]) -> None:
    """A brief as it ARRIVES by sync, claiming the captures it consumed."""
    from aiforge_core.memory import md_store

    src = "".join(f"  - {s}\n" for s in sources)
    (md_store.briefs_dir() / f"compacted-{key}.md").write_text(
        f"---\ntype: knowledge\nkey: {key}\nresource: ''\n"
        f"timestamp: 2026-07-19T00:00:00Z\ntags: []\nlinks: []\n"
        + (f"sources:\n{src}" if sources else "")
        + f"---\n\n# {key}\n\n## Facts\n\n- distilled\n", encoding="utf-8")


def _compact(**kw) -> dict:
    from aiforge_core.memory import md_store

    return md_store.compact(summarize=False, **kw)


def _captures(tmp_path) -> set[str]:
    from aiforge_core.memory import md_store

    return {p.stem for p in md_store.captures_dir().glob("*.md")}


# ── distillation: everywhere ─────────────────────────────────────────────
def test_distillation_runs_on_a_spoke_too(spoke):
    """A spoke is not a thin client: it distils its own captures with its own
    context, and the briefs stay on it."""
    _capture(spoke, "one")
    _capture(spoke, "two")

    out = _compact()

    assert "skipped" not in out
    assert out["files_out"] >= 1


def test_distillation_runs_on_the_admin(mem):
    _capture(mem, "one")
    _capture(mem, "two")

    assert "skipped" not in _compact()


def test_distillation_runs_on_a_single_machine(mem):
    """No admin url = we are the admin = nothing to defer to. Unchanged."""
    _capture(mem, "one")
    _capture(mem, "two")

    assert "skipped" not in _compact()


def test_a_garbage_role_still_lets_us_distil(mem, monkeypatch):
    """Nothing about the role reaches local compaction — not even a broken one."""
    _capture(mem, "one")
    _capture(mem, "two")
    monkeypatch.setenv("AIFORGE_ROLE", "leader")

    assert "skipped" not in _compact()


def test_a_dry_run_preview_is_a_preview_everywhere(spoke):
    """It reads and costs no tokens, so it never writes — on any machine."""
    _capture(spoke, "one")
    _capture(spoke, "two")

    out = _compact(dry_run=True)

    assert out["dry_run"] is True
    assert "skipped" not in out
    assert _captures(spoke) == {"one", "two"}


# ── housekeeping: a brief that arrived claims captures we also hold ───────
def test_an_arrived_brief_lets_us_archive_exactly_what_it_covers(spoke):
    """``archive_covered_captures`` reached directly: the normal path archives
    what OUR OWN fold just consumed, and this is the other half."""
    from aiforge_core.memory.md_store import archive_covered_captures

    for stem in ("cap-x", "cap-y", "cap-z"):
        _capture(spoke, stem)
    _brief(spoke, "shared", ["cap-x", "cap-y"])

    out = archive_covered_captures()

    assert out["archived"] == 2
    assert _captures(spoke) == {"cap-z"}, "an uncovered capture must survive"


def test_a_brief_with_no_provenance_archives_nothing(spoke):
    """Briefs written before provenance existed claim nothing — and guessing
    which captures they ate would delete un-distilled memory."""
    from aiforge_core.memory.md_store import archive_covered_captures

    mem = spoke
    for stem in ("cap-x", "cap-y"):
        _capture(mem, stem)
    _brief(mem, "shared", [])

    out = archive_covered_captures()

    assert out["archived"] == 0
    assert _captures(mem) == {"cap-x", "cap-y"}


def test_a_provenance_check_that_explodes_archives_nothing(spoke, monkeypatch):
    """Soft-fail CLOSED: any doubt at all → move nothing."""
    from aiforge_core.memory.md_store import _compact as _c
    from aiforge_core.memory.md_store import archive_covered_captures

    mem = spoke
    for stem in ("cap-x", "cap-y"):
        _capture(mem, stem)
    _brief(mem, "shared", ["cap-x", "cap-y"])
    monkeypatch.setattr(_c, "brief_source_stems",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))

    out = archive_covered_captures()

    assert out["archived"] == 0
    assert out["housekeeping"] == "provenance-unreadable"
    assert _captures(mem) == {"cap-x", "cap-y"}


def test_the_admin_still_archives_by_distilling_not_by_housekeeping(mem):
    _capture(mem, "cap-x")
    _capture(mem, "cap-y")

    out = _compact()

    assert out["files_in"] == 2
    assert _captures(mem) == set()


# ── provenance is actually WRITTEN by the fold ───────────────────────────
def test_the_fold_records_the_capture_stems_it_consumed(mem):
    from aiforge_core.memory import md_store
    _capture(mem, "cap-x")
    _capture(mem, "cap-y")

    _compact()

    assert md_store.brief_source_stems() == {"cap-x", "cap-y"}


def test_a_knowledge_axis_fold_records_them_too(mem):
    from aiforge_core.memory import md_store
    _capture(mem, "cap-x", repo="acme")
    _capture(mem, "cap-y", repo="acme")

    _compact(group_by="repo")

    assert md_store.brief_source_stems() == {"cap-x", "cap-y"}


def test_projection_mode_claims_nothing_it_did_not_archive(mem):
    """archive_sources=False keeps the raw units alive for the OTHER axis, so a
    peer must not tidy them away on this brief's word."""
    from aiforge_core.memory import md_store
    _capture(mem, "cap-x", repo="acme")
    _capture(mem, "cap-y", repo="acme")

    _compact(group_by="repo", archive_sources=False)

    assert md_store.brief_source_stems() == set()


def test_a_write_time_fact_fold_keeps_the_provenance(mem):
    """_brief_upsert re-renders the brief on every capture; a dropped
    ``sources`` would un-claim captures a peer is waiting to archive."""
    from aiforge_core.memory import md_store
    _brief(mem, "acme", ["cap-x"])

    md_store._brief_upsert("acme", "a brand new fact")

    assert md_store.brief_source_stems() == {"cap-x"}
