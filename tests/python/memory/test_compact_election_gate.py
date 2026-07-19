"""compact() asks the sync election two questions, with opposite bias.

"May I distil?" fails OPEN — a duplicate brief is cheap. "Which captures does an
arrived brief already cover?" fails CLOSED — an archived capture no brief covers
is unrecoverable. Single-machine behaviour must be byte-for-byte what it was
before the mesh existed: no peers configured, no gate.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    return tmp_path


def _approve(peer_id: str = "air", *, ago: int = 60) -> None:
    """An approved peer we reached ``ago`` seconds back. 'air' < 'nuc'."""
    from aiforge_core.memory.sync import peers

    data = peers.load()
    data["peers"] = [{"id": peer_id, "urls": ["http://10.0.0.9:8799"],
                      "state": peers.STATE_APPROVED,
                      "last_seen": int(time.time()) - ago}]
    peers.save(data)


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


# ── distillation: leader-only ────────────────────────────────────────────
def test_distillation_is_skipped_on_a_non_leader(mem):
    _approve()
    _capture(mem, "one")
    _capture(mem, "two")

    out = _compact()

    assert out["skipped"] == "not-leader"
    assert out["files_out"] == 0


def test_distillation_runs_when_we_are_the_leader(mem):
    _approve("zed")            # 'nuc' < 'zed', so we lead
    _capture(mem, "one")
    _capture(mem, "two")

    assert "skipped" not in _compact()


def test_distillation_runs_on_a_single_machine(mem):
    """No approved peers = no mesh = nothing to defer to. Unchanged behaviour."""
    _capture(mem, "one")
    _capture(mem, "two")

    assert "skipped" not in _compact()


def test_an_election_that_explodes_makes_us_distil_anyway(mem, monkeypatch):
    """Losing compaction to an unreadable registry is worse than duplicating it."""
    from aiforge_core.memory.sync import election
    _approve()
    _capture(mem, "one")
    _capture(mem, "two")
    monkeypatch.setattr(election, "is_leader",
                        lambda: (_ for _ in ()).throw(OSError("config gone")))

    assert "skipped" not in _compact()


def test_a_dry_run_preview_is_never_gated(mem):
    """It reads, costs no tokens, and an operator asking "what would happen"
    deserves an answer even on a follower."""
    _approve()
    _capture(mem, "one")
    _capture(mem, "two")

    out = _compact(dry_run=True)

    assert out["dry_run"] is True and "skipped" not in out


# ── housekeeping: everybody's job ────────────────────────────────────────
def test_a_non_leader_archives_exactly_what_an_arrived_brief_covers(mem):
    _approve()
    for stem in ("cap-x", "cap-y", "cap-z"):
        _capture(mem, stem)
    _brief(mem, "shared", ["cap-x", "cap-y"])

    out = _compact()

    assert out["skipped"] == "not-leader"
    assert out["archived"] == 2
    assert _captures(mem) == {"cap-z"}, "an uncovered capture must survive"


def test_a_brief_with_no_provenance_archives_nothing(mem):
    """Briefs written before provenance existed claim nothing — and guessing
    which captures they ate would delete un-distilled memory."""
    _approve()
    for stem in ("cap-x", "cap-y"):
        _capture(mem, stem)
    _brief(mem, "shared", [])

    out = _compact()

    assert out["archived"] == 0
    assert _captures(mem) == {"cap-x", "cap-y"}


def test_a_provenance_check_that_explodes_archives_nothing(mem, monkeypatch):
    """Soft-fail CLOSED — the opposite of the distillation gate, on purpose."""
    from aiforge_core.memory.md_store import _compact as _c
    _approve()
    for stem in ("cap-x", "cap-y"):
        _capture(mem, stem)
    _brief(mem, "shared", ["cap-x", "cap-y"])
    monkeypatch.setattr(_c, "brief_source_stems",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))

    out = _compact()

    assert out["archived"] == 0
    assert out["housekeeping"] == "provenance-unreadable"
    assert _captures(mem) == {"cap-x", "cap-y"}


def test_the_leader_still_archives_by_distilling_not_by_housekeeping(mem):
    _capture(mem, "cap-x")
    _capture(mem, "cap-y")

    out = _compact()

    assert out["files_in"] == 2 and _captures(mem) == set()


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
