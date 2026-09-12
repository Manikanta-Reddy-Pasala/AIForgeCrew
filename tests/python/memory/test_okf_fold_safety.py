"""What a fold must never destroy.

An end-to-end read of the OKF store found several ways a brief could lose
knowledge nothing could rebuild: a fold with no model rendering `facts=[]`, a
brief's own facts fed back as one blob and collapsing into a single run-on
fact, a merge deleting its members before the merged file landed, and a
sibling-name glob absorbing another brief. Each test here pins one of those.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def _write_brief(key: str, facts: list[str], **kw):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes
    p = md_store.brief_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(work_notes.render_note(
        "knowledge", key, title=f"{key} memory (compacted)",
        objective="Durable knowledge.", facts=facts, **kw), encoding="utf-8")
    return p


# ── a fold without a model keeps the Facts it found ──────────────────────

def test_a_fold_with_no_model_keeps_the_briefs_facts(mem):
    """summarize=False rendered `facts=[]`, so a boot with compaction off (or
    with a ticket still running) erased every touched brief's knowledge."""
    from aiforge_core.memory import md_store
    _write_brief("svc", ["port is 8799", "auth is basic"])
    md_store.write("a note", "the cache is redis", kind="note", repo="svc",
                   ingest=False)
    md_store.compact(group_by="repo", min_group=1, summarize=False,
                     archive_sources=False)
    text = md_store.brief_path("svc").read_text(encoding="utf-8")
    assert "port is 8799" in text
    assert "auth is basic" in text
    assert "the cache is redis" in text       # and the new note landed


def test_a_fold_with_no_model_keeps_links_and_key_results(mem):
    from aiforge_core.memory import md_store
    _write_brief("svc", ["port is 8799"], links=["[global](compacted-shared.md)"],
                 key_results=["PROJ-12 shipped"])
    md_store.compact(group_by="repo", min_group=1, summarize=False,
                     archive_sources=False, force=True)
    text = md_store.brief_path("svc").read_text(encoding="utf-8")
    assert "compacted-shared.md" in text
    assert "PROJ-12 shipped" in text


# ── a brief fed back to itself does not collapse into one fact ───────────

def test_a_bullet_list_folds_back_as_one_fact_per_bullet():
    from aiforge_core.runtime.work_notes._consolidate import _blob_facts
    assert _blob_facts("- a fact\n- another fact\n- a third") == [
        "a fact", "another fact", "a third"]


def test_prose_still_folds_as_one_fact():
    from aiforge_core.runtime.work_notes._consolidate import _blob_facts
    assert _blob_facts("a sentence\nrunning over two lines") == [
        "a sentence running over two lines"]


def test_a_negation_is_not_swallowed_by_the_fact_it_negates():
    """Containment dropped "retries 3x" because "no retries 3x" contains it —
    deleting a fact and keeping its opposite."""
    from aiforge_core.runtime.work_notes._consolidate import _dedupe_ci
    assert _dedupe_ci(["retries 3x", "no retries 3x"]) == [
        "retries 3x", "no retries 3x"]
    # a genuine extension is still collapsed
    assert _dedupe_ci(["status: Done", "status: Done (auto)"]) == [
        "status: Done (auto)"]


def test_a_fold_that_returns_almost_nothing_keeps_the_old_facts():
    from aiforge_core.memory.md_store._compact import _kept_facts
    old = [f"fact {i}" for i in range(10)]
    assert _kept_facts([], old, "svc") == old            # empty ⇒ failed fold
    assert _kept_facts(["one"], old, "svc")[-1] == "one"  # 1 of 10 ⇒ union back
    kept = _kept_facts(["a", "b", "c", "d"], old, "svc")
    assert kept == ["a", "b", "c", "d"]                  # a real consolidation


# ── a brief never absorbs its namesakes ──────────────────────────────────

def test_only_numeric_parts_count_as_parts_of_a_brief(mem):
    from aiforge_core.memory.md_store._base import _brief_part_paths
    _write_brief("auth", ["x"])
    _write_brief("auth-2", ["part two"])
    _write_brief("auth-service", ["another brief entirely"])
    names = [p.stem for p in _brief_part_paths("auth")]
    assert names == ["compacted-auth-2"]


# ── one scope rule, both ingest paths ────────────────────────────────────

def test_a_topic_brief_is_repo_agnostic_and_a_repo_brief_is_not(mem,
                                                                monkeypatch):
    from aiforge_core.memory.md_store import _topics
    monkeypatch.setattr(_topics, "_repo_brief_names", lambda: {"posbackend"})
    assert _topics.brief_repo_scope("posbackend", "topic") == "posbackend"
    assert _topics.brief_repo_scope("shared", "topic") == "shared"
    assert _topics.brief_repo_scope("rate-limiting", "topic") is None
    # a repo none of the discovery sources knows is recognised by its own tag
    assert _topics.brief_repo_scope("vidpipe", "topic",
                                    ["repo:vidpipe"]) == "vidpipe"


def test_the_force_pass_folds_each_brief_on_its_own_axis(mem, monkeypatch):
    from aiforge_core.memory.md_store import _compact, _topics
    monkeypatch.setattr(_topics, "_repo_brief_names", lambda: {"svc"})
    _write_brief("svc", ["a repo fact"])
    _write_brief("rate-limiting", ["a topic fact"])
    _write_brief("shared", ["a global fact"])
    repo_groups: dict = {}
    topic_groups: dict = {}
    _compact._add_existing_briefs(repo_groups, "repo")
    _compact._add_existing_briefs(topic_groups, "topic")
    assert set(repo_groups) == {"svc", "shared"}
    assert set(topic_groups) == {"rate-limiting"}


# ── the merge writes before it deletes ───────────────────────────────────

def test_a_failed_merge_write_keeps_every_member(mem, monkeypatch):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store._graph import _reconcile
    _write_brief("gpsd", ["canonical fact"])
    _write_brief("gpsd-config", ["member fact"])
    monkeypatch.setattr(_reconcile, "_write_brief_file",
                        lambda *_a, **_k: False)       # the write fails
    assert _reconcile._merge_cluster(["gpsd", "gpsd-config"], set()) == 0
    assert md_store.brief_path("gpsd-config").exists()
    assert "member fact" in md_store.brief_path("gpsd-config").read_text(
        encoding="utf-8")


def test_a_family_merge_never_mints_a_generic_topic(mem):
    """`api` + `api-gateway` share a first word; that is not a reason to create
    compacted-api.md, the magnet the topic vocabulary exists to keep out."""
    from aiforge_core.memory.md_store._graph import _reconcile
    assert _reconcile._canonical_name(["api-gateway", "api-limits"], set()) \
        == "api-gateway"
    assert _reconcile._canonical_name(["windows-ntp", "windows-cpu-mode"],
                                      set()) == "windows"


def test_a_merged_member_is_archived_not_deleted(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store._base import memory_dir
    from aiforge_core.memory.md_store._graph import _reconcile
    _write_brief("gpsd", ["canonical fact"])
    _write_brief("gpsd-config", ["member fact"])
    assert _reconcile._merge_cluster(["gpsd", "gpsd-config"], set()) == 1
    assert not md_store.brief_path("gpsd-config").exists()
    archived = list((memory_dir() / "archive").rglob("compacted-gpsd-config.md"))
    assert archived, "the member must survive in archive/"
    assert "member fact" in md_store.brief_path("gpsd").read_text(
        encoding="utf-8")


# ── the write-time upsert rewrites the WHOLE brief — it must lose nothing ─

def test_a_capture_keeps_the_briefs_links_and_tags(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store._render import _brief_upsert
    _write_brief("svc", ["port is 8799"], links=["[global](compacted-shared.md)"],
                 tags=["repo:svc", "topic:ports"])
    _brief_upsert("svc", "the cache is redis")
    text = md_store.brief_path("svc").read_text(encoding="utf-8")
    assert "compacted-shared.md" in text
    assert "repo:svc" in text
    assert "the cache is redis" in text


def test_facts_past_the_cap_move_into_the_body_not_the_bin():
    from aiforge_core.memory.md_store import _render
    facts = [f"fact number {i} " + "x" * 100 for i in range(400)]
    body = _render._bound_facts(facts, "")
    assert len(facts) < 400                      # some were moved out
    assert "fact number 0" in body               # …into the body, not deleted


def test_a_correction_is_recorded_even_when_a_longer_fact_quotes_it(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store._render import _brief_upsert
    _write_brief("svc", ["no retries 3x on the ingest path"])
    _brief_upsert("svc", "retries 3x")
    assert "retries 3x" in md_store.brief_path("svc").read_text(encoding="utf-8")


# ── the semantic dedupe respects scope ───────────────────────────────────

def test_the_dedupe_sweep_keeps_one_copy_per_repo(mem, monkeypatch):
    from aiforge_core.memory import sqlite_memory
    from aiforge_core.memory.sqlite_memory import _maintenance
    monkeypatch.setattr(_maintenance.local_embed, "cosine", lambda _a, _b: 1.0)
    for repo in ("alpha", "beta"):
        sqlite_memory.write_unit(text=f"the queue is nats ({repo})",
                                 kind="note", source=f"md:{repo}", repo=repo)
    out = _maintenance.dedupe()
    assert out["removed"] == 0, "facts in different repos are different facts"
