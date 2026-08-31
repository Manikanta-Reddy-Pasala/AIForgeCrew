"""The memory admin API: search, md files, sources, indexing, and clearing.

Three properties carry most of the risk here. Indexing is CPU-bound, so it is
spawned as a separate PROCESS — in an api thread it starves the event loop and
wedges every request for minutes. Clearing a store DELETES a user's memory, so
it requires an explicit confirm. And search reports both a flat, deduped list
(what agents consume) and per-origin groups built from the PRE-dedup ranking,
so a brief matched by both the vector index and the keyword index shows in
both buckets instead of one hiding behind the other.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import memory as mem


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(mem.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def quiet_state():
    mem._compact_all_state.update(running=False, started_at=None, steps=[],
                                  current=None, sub=None, done=False,
                                  result=None, error=None)
    mem._reindex_all_at[0] = 0.0
    yield
    mem._reindex_all_at[0] = 0.0


# ─── stats ─────────────────────────────────────────────────────────────


def test_stats_report_the_embedded_backend(client, monkeypatch):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setattr(sqlite_memory, "stats",
                        lambda: {"total": 7, "by_kind": {"learning": 5, "note": 2}})
    monkeypatch.delenv("AIFORGE_OKR_DAG", raising=False)
    body = client.get("/api/memory/stats").json()
    assert body["backend"] == "sqlite" and body["total"] == 7
    assert {w["wing"]: w["n"] for w in body["wings"]} == {"learning": 5, "note": 2}
    assert body["okr_dag"] is False


def test_the_dag_panel_is_flagged_when_enabled(client, monkeypatch):
    from aiforge_core.memory import sqlite_memory
    monkeypatch.setattr(sqlite_memory, "stats", lambda: {"total": 0, "by_kind": {}})
    monkeypatch.setenv("AIFORGE_OKR_DAG", "1")
    assert client.get("/api/memory/stats").json()["okr_dag"] is True


# ─── search ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hit,origin", [
    ({"channel": "memory"}, "vector"),
    ({"channel": "vector"}, "vector"),
    ({"channel": "keyword"}, "md"),
    ({"channel": "linked"}, "md"),
    ({"source": "compacted:topic"}, "md"),
    ({"source": "md:notes"}, "md"),
    ({"source": "ticket:ONE-1"}, "other"),
    ({}, "other"),
])
def test_hits_are_bucketed_by_origin(hit, origin):
    assert mem._search_origin(hit) == origin


@pytest.fixture()
def search(monkeypatch):
    from aiforge_core.memory import unified_query
    state: dict = {}

    def _query(q, role=None, limit=None):
        state.update(q=q, role=role, limit=limit)
        return state["result"]
    monkeypatch.setattr(unified_query, "query", _query)
    return state


def test_search_returns_both_groups_and_a_flat_list(client, search):
    search["result"] = {
        "used_sources": ["md"],
        "hits": [{"channel": "memory", "text": "a", "score": 1.0}],
        "ranked": [{"channel": "memory", "text": "a", "score": 1.0},
                   {"channel": "keyword", "text": "a", "score": 0.9}],
    }
    body = client.get("/api/memory/search?q=lru").json()
    assert body["query"] == "lru" and body["used_sources"] == ["md"]
    assert len(body["hits"]) == 1
    # the SAME brief appears in both buckets — overlap is expected
    assert len(body["groups"]["vector"]) == 1 and len(body["groups"]["md"]) == 1
    assert body["hits"][0]["origin"] == "vector"


def test_duplicates_within_a_bucket_are_collapsed(client, search):
    search["result"] = {"hits": [], "ranked": [
        {"channel": "memory", "text": "same"},
        {"channel": "memory", "text": "same"},
    ]}
    assert len(client.get("/api/memory/search?q=lru").json()["groups"]["vector"]) == 1


def test_each_bucket_is_capped_at_top_k(client, search):
    search["result"] = {"hits": [], "ranked": [
        {"channel": "memory", "text": f"hit {i}"} for i in range(20)]}
    body = client.get("/api/memory/search?q=lru&top_k=3").json()
    assert len(body["groups"]["vector"]) == 3


def test_a_row_carries_its_metadata_and_is_text_capped(client, search):
    search["result"] = {"hits": [{"channel": "memory", "text": "x" * 2000,
                                  "ticket": "ONE-1", "repo": "app",
                                  "kind": "learning", "linked": True}],
                        "ranked": []}
    row = client.get("/api/memory/search?q=lru").json()["hits"][0]
    assert len(row["text"]) == 800
    assert row["metadata"] == {"ticket": "ONE-1", "repo": "app"}
    assert row["wing"] == "learning" and row["linked"] is True


def test_ranked_falls_back_to_hits(client, search):
    search["result"] = {"hits": [{"channel": "keyword", "text": "a"}]}
    assert len(client.get("/api/memory/search?q=lru").json()["groups"]["md"]) == 1


def test_a_one_character_query_is_rejected(client):
    assert client.get("/api/memory/search?q=x").status_code == 422


def test_the_top_k_ceiling_is_enforced(client):
    assert client.get("/api/memory/search?q=lru&top_k=500").status_code == 422


# ─── md files ──────────────────────────────────────────────────────────


def test_files_are_listed(client, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "list_files", lambda: [{"name": "a.md"}])
    assert client.get("/api/memory/files").json() == [{"name": "a.md"}]


def test_one_file_is_read(client, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "read_file", lambda name: {"name": name, "text": "x"})
    assert client.get("/api/memory/files/a.md").json()["text"] == "x"


def test_a_missing_file_is_a_404(client, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "read_file", lambda name: None)
    assert client.get("/api/memory/files/gone.md").status_code == 404


def test_a_file_is_created_as_manual(client, monkeypatch):
    from aiforge_core.memory import md_store
    seen: dict = {}

    def _write(title, text, kind="note", tags=(), source=""):
        seen.update(title=title, text=text, kind=kind, tags=list(tags), source=source)
        return {"ok": True}
    monkeypatch.setattr(md_store, "write", _write)
    r = client.post("/api/memory/files",
                    json={"title": "t", "text": "b", "tags": ["x"]})
    assert r.status_code == 201
    assert seen == {"title": "t", "text": "b", "kind": "note",
                    "tags": ["x"], "source": "manual"}


def test_a_file_needs_a_title_and_text(client):
    assert client.post("/api/memory/files", json={"title": "", "text": "b"}).status_code == 422


def test_a_file_is_deleted(client, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "delete_file", lambda name: True)
    assert client.delete("/api/memory/files/a.md").json() == {"deleted": True,
                                                              "name": "a.md"}


def test_the_whole_dir_is_reingested(client, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "ingest_dir", lambda: {"units": 12})
    assert client.post("/api/memory/files/ingest").json() == {"units": 12}


def test_legacy_briefs_are_tidied(client, monkeypatch):
    from aiforge_core.memory import md_store
    seen: dict = {}

    def _cleanup(dry_run=False, model_role=""):
        seen.update(dry_run=dry_run, model_role=model_role)
        return {"folded": 3}
    monkeypatch.setattr(md_store, "cleanup_legacy_compacted", _cleanup)
    client.post("/api/memory/files/cleanup?dry_run=true")
    assert seen == {"dry_run": True, "model_role": "learner"}


# ─── compaction ────────────────────────────────────────────────────────


@pytest.fixture()
def compact(monkeypatch):
    from aiforge_core.memory import md_store
    seen: dict = {}

    def _compact(**kw):
        seen.update(kw)
        return {"files_in": 4}
    monkeypatch.setattr(md_store, "compact", _compact)
    monkeypatch.setattr(md_store, "finalize_briefs",
                        lambda role=None: {"merged": 1, "role": role})
    return seen


def test_a_plain_compact_also_runs_the_cross_brief_rules(client, compact):
    """The button rewrites with ALL the rules, not just the fold."""
    body = client.post("/api/memory/files/compact").json()
    assert body["files_in"] == 4
    assert body["rules"] == {"merged": 1, "role": "learner"}
    assert compact["group_by"] == "topic" and compact["summarize"] is True


def test_a_dry_run_never_applies_the_rules(client, compact, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "finalize_briefs",
                        lambda role=None: pytest.fail("mutated during a dry run"))
    assert "rules" not in client.post(
        "/api/memory/files/compact?dry_run=true").json()


def test_the_forced_path_skips_the_rules(client, compact, monkeypatch):
    """'Compact all' runs them as its own steps."""
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "finalize_briefs",
                        lambda role=None: pytest.fail("ran the rules twice"))
    client.post("/api/memory/files/compact?force=true")


def test_a_failing_rules_pass_is_reported_not_raised(client, compact, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "finalize_briefs",
                        lambda role=None: (_ for _ in ()).throw(RuntimeError("no llm")))
    assert client.post("/api/memory/files/compact").json()["rules"] == {
        "error": "no llm"}


def test_dedupe_delegates_to_the_migration(client, monkeypatch):
    from aiforge_core.memory import migrations
    monkeypatch.setattr(migrations, "dedupe_all", lambda: {"chat": {"removed": 2}})
    assert client.post("/api/memory/dedupe").json()["chat"]["removed"] == 2


# ─── compact-all (background) ──────────────────────────────────────────


@pytest.fixture()
def recompact(monkeypatch):
    """Run the heavy pass inline so the progress plumbing is observable."""
    from aiforge_core.memory import migrations
    state: dict = {}

    def _force(on_step=None):
        state["on_step"] = on_step
        on_step("topic", "run", None)
        on_step("topic", "progress", {"done": 2, "total": 5, "key": "lru"})
        on_step("topic", "done", {"ok": True})
        if state.get("boom"):
            raise RuntimeError("no llm")
        return {"ok": True}
    monkeypatch.setattr(migrations, "force_recompact_all", _force)
    monkeypatch.setattr(mem, "_spawn", lambda fn, name=None: fn())
    return state


def test_compact_all_runs_in_the_background_and_reports_progress(client, recompact):
    assert client.post("/api/memory/compact-all").json() == {"ok": True,
                                                             "started": True}
    body = client.get("/api/memory/compact-all/status").json()
    assert body["done"] is True and body["running"] is False
    assert body["steps_done"] == ["topic"]
    assert body["result"] == {"ok": True}
    assert body["total_steps"] == 15


def test_a_second_request_does_not_start_a_second_pass(client, recompact):
    mem._compact_all_state.update(running=True, current="topic")
    body = client.post("/api/memory/compact-all").json()
    assert body["already_running"] is True and body["current"] == "topic"


def test_a_crash_is_surfaced_in_the_status(client, recompact):
    recompact["boom"] = True
    client.post("/api/memory/compact-all")
    body = client.get("/api/memory/compact-all/status").json()
    assert body["error"] == "no llm" and body["done"] is True


def test_the_status_of_an_idle_run(client):
    body = client.get("/api/memory/compact-all/status").json()
    assert body == {"running": False, "done": False, "current": None, "sub": None,
                    "steps_done": [], "total_steps": 15, "error": None,
                    "elapsed_s": 0, "result": None}


# ─── the OKR graph ─────────────────────────────────────────────────────


def test_the_graph_returns_previews_not_full_bodies(client, monkeypatch):
    from aiforge_core.memory import okf

    class _G:
        nodes = {"n1": {"type": "learning", "body": "x" * 500,
                        "meta": {"title": "T", "status": "open", "tags": ["a"]}}}

        def counts(self):
            return {"learning": 1}
    monkeypatch.setattr(okf, "build", lambda force=False: _G())
    monkeypatch.setattr(okf, "get_active", lambda: "kr-1")
    body = client.get("/api/memory/okf").json()
    assert body["counts"] == {"learning": 1} and body["active_kr"] == "kr-1"
    assert len(body["nodes"][0]["preview"]) == 200
    assert body["nodes"][0]["title"] == "T"


def test_a_node_without_a_title_falls_back_to_its_id(client, monkeypatch):
    from aiforge_core.memory import okf

    class _G:
        nodes = {"n1": {"type": "learning", "body": "", "meta": {}}}

        def counts(self):
            return {}
    monkeypatch.setattr(okf, "build", lambda force=False: _G())
    monkeypatch.setattr(okf, "get_active", lambda: None)
    assert client.get("/api/memory/okf").json()["nodes"][0]["title"] == "n1"


def test_the_active_kr_can_be_set(client, monkeypatch):
    from aiforge_core.memory import okf
    seen: dict = {}

    def _set_active(kr):
        seen["kr"] = kr
        return {"ok": True}
    monkeypatch.setattr(okf, "set_active", _set_active)
    client.post("/api/memory/okf/active", json={"active_kr": "kr-2"})
    assert seen["kr"] == "kr-2"


def test_the_graph_is_seeded_from_the_briefs(client, monkeypatch):
    from aiforge_core.memory import okf
    monkeypatch.setattr(okf, "migrate_from_briefs", lambda: {"seeded": 4})
    assert client.post("/api/memory/okf/migrate").json() == {"seeded": 4}


# ─── sources + indexing ────────────────────────────────────────────────


@pytest.fixture()
def sources(monkeypatch):
    from aiforge_core.runtime import memory_sources as ms
    state: dict = {"rows": [{"id": 1, "kind": "repo"}], "spawned": [],
                   "status": []}
    monkeypatch.setattr(ms, "list_sources", lambda: state["rows"])
    monkeypatch.setattr(ms, "create",
                        lambda kind, location, name: {"id": 2, "kind": kind,
                                                      "location": location,
                                                      "name": name})
    monkeypatch.setattr(ms, "get", lambda sid: state["rows"][0] if sid == 1 else None)
    monkeypatch.setattr(ms, "delete", lambda sid: sid == 1)
    monkeypatch.setattr(ms, "set_status",
                        lambda sid, st, error=None: state["status"].append((sid, st)))
    monkeypatch.setattr(mem, "_spawn_index", lambda sid: state["spawned"].append(sid))
    return state


def test_sources_are_listed(client, sources):
    assert client.get("/api/memory/sources").json() == [{"id": 1, "kind": "repo"}]


def test_a_repo_source_starts_indexing_immediately(client, sources):
    r = client.post("/api/memory/sources",
                    json={"kind": "repo", "location": "/repo"})
    assert r.status_code == 201 and r.json()["status"] == "indexing"
    assert sources["spawned"] == [2] and sources["status"] == [(2, "indexing")]


def test_a_file_source_stays_manual(client, sources):
    r = client.post("/api/memory/sources",
                    json={"kind": "file", "location": "/tmp/a.txt"})
    assert r.status_code == 201 and "status" not in r.json()
    assert sources["spawned"] == []


def test_an_invalid_source_is_a_400(client, sources, monkeypatch):
    from aiforge_core.runtime import memory_sources as ms
    monkeypatch.setattr(ms, "create",
                        lambda *a: (_ for _ in ()).throw(ValueError("bad kind")))
    r = client.post("/api/memory/sources", json={"kind": "nope", "location": "x"})
    assert r.status_code == 400 and "bad kind" in r.json()["detail"]


def test_indexing_an_existing_source(client, sources):
    assert client.post("/api/memory/sources/1/index").json()["status"] == "indexing"
    assert sources["spawned"] == [1]


def test_indexing_a_missing_source_is_a_404(client, sources):
    assert client.post("/api/memory/sources/99/index").status_code == 404


def test_deleting_a_source(client, sources):
    assert client.delete("/api/memory/sources/1").status_code == 204
    assert client.delete("/api/memory/sources/99").status_code == 404


def test_an_upload_is_saved_and_registered(client, sources, monkeypatch, tmp_path):
    monkeypatch.setattr(mem, "config_dir", lambda: tmp_path)
    r = client.post("/api/memory/sources/upload",
                    files={"file": ("notes.txt", b"hello")})
    assert r.status_code == 201 and r.json()["kind"] == "file"
    assert (tmp_path / "memory-files" / "notes.txt").read_bytes() == b"hello"


def test_an_upload_filename_cannot_traverse(client, sources, monkeypatch, tmp_path):
    monkeypatch.setattr(mem, "config_dir", lambda: tmp_path)
    client.post("/api/memory/sources/upload",
                files={"file": ("../../evil.txt", b"x")})
    assert (tmp_path / "memory-files" / "evil.txt").exists()


def test_reindex_all_counts_only_indexable_sources(client, sources, monkeypatch):
    sources["rows"] = [{"id": 1, "kind": "repo"}, {"id": 2, "kind": "docs"},
                       {"id": 3, "kind": "url"}]
    spawned: list = []
    monkeypatch.setattr(mem, "_spawn_reindex_all", lambda: spawned.append(1))
    assert client.post("/api/memory/reindex-all").json() == {"ok": True,
                                                             "reindexing": 2}
    assert spawned == [1]


def test_indexing_runs_in_a_separate_process(monkeypatch):
    """In an api thread the CPU-bound index starves the event loop and wedges
    every request for minutes."""
    from aiforge_core.runtime import background as bg
    seen: dict = {}

    def _spawn(name=None, kind=None, argv=None, **kw):
        seen.update(name=name, kind=kind, argv=argv)
        return object()
    monkeypatch.setattr(bg, "spawn", _spawn)
    mem._spawn_index(7)
    assert seen["kind"] == "process" and seen["argv"][-1] == "7"
    assert "aiforge_core.runtime.memory_ingest" in seen["argv"]


def test_a_failed_process_launch_falls_back_to_a_thread(monkeypatch):
    from aiforge_core.runtime import background as bg
    calls: list = []

    def _spawn(*a, **kw):
        calls.append(kw.get("kind"))
        return None if kw.get("kind") == "process" else object()
    monkeypatch.setattr(bg, "spawn", _spawn)
    mem._spawn_index(7)
    assert calls == ["process", None]


def test_a_full_sweep_is_debounced(monkeypatch):
    """The daily fire and a manual trigger landing together must not launch two
    full sweeps."""
    from aiforge_core.runtime import background as bg
    spawns: list = []
    monkeypatch.setattr(bg, "spawn", lambda **kw: spawns.append(kw.get("name")))
    mem._spawn_reindex_all()
    mem._spawn_reindex_all()
    assert spawns == ["reindex-all"]


# ─── validate-path ─────────────────────────────────────────────────────


def test_a_path_is_validated_before_indexing(client, monkeypatch):
    """A wrong/empty/relative path is the #1 cause of an index that silently
    produces 0 units."""
    import aiforge_core.runtime.memory_ingest as mi
    monkeypatch.setattr(mi, "validate_path",
                        lambda loc: {"ok": True, "abs": "/repo", "code_files": 12})
    assert client.post("/api/memory/validate-path",
                       json={"location": "/repo"}).json()["code_files"] == 12


# ─── overview, graph samples, clearing ─────────────────────────────────


def test_the_overview_is_served_from_admin(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "memory_overview", lambda: {"sqlite": {"units": 3}})
    assert client.get("/api/memory/overview").json()["sqlite"]["units"] == 3


def test_a_graph_sample_is_served(client, monkeypatch):
    from aiforge_core.memory import admin
    seen: dict = {}

    def _sample(store, limit):
        seen.update(store=store, limit=limit)
        return {"available": True, "nodes": [], "edges": []}
    monkeypatch.setattr(admin, "graph_sample", _sample)
    client.get("/api/memory/graph?store=symbols&limit=10")
    assert seen == {"store": "symbols", "limit": 10}


def test_the_graph_sample_limit_is_capped(client):
    assert client.get("/api/memory/graph?store=symbols&limit=999").status_code == 422


def test_a_node_neighbourhood_is_served(client, monkeypatch):
    from aiforge_core.memory import admin
    seen: dict = {}

    def _expand(store, node_id, limit):
        seen.update(store=store, node_id=node_id, limit=limit)
        return {"available": True, "nodes": [], "edges": []}
    monkeypatch.setattr(admin, "graph_expand", _expand)
    client.get("/api/memory/graph/expand?store=chunks&node_id=n1&limit=5")
    assert seen == {"store": "chunks", "node_id": "n1", "limit": 5}


def test_clearing_a_store_requires_confirmation(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "clear_store",
                        lambda store: pytest.fail("cleared without confirm"))
    r = client.post("/api/memory/clear/sqlite", json={"confirm": False})
    assert r.status_code == 400 and "confirm=true" in r.json()["detail"]


def test_a_confirmed_clear_runs(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "clear_store", lambda store: {"cleared": store})
    assert client.post("/api/memory/clear/sqlite",
                       json={"confirm": True}).json() == {"cleared": "sqlite"}


def test_an_unknown_store_is_a_400(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "clear_store",
                        lambda store: (_ for _ in ()).throw(ValueError("unknown store")))
    r = client.post("/api/memory/clear/nope", json={"confirm": True})
    assert r.status_code == 400


def test_wiping_everything_requires_confirmation(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "clear_all", lambda: pytest.fail("wiped without confirm"))
    assert client.post("/api/memory/clear-all", json={}).status_code == 400


def test_a_confirmed_wipe_runs(client, monkeypatch):
    from aiforge_core.memory import admin
    monkeypatch.setattr(admin, "clear_all", lambda: {"ok": True})
    assert client.post("/api/memory/clear-all", json={"confirm": True}).json() == {
        "ok": True}
