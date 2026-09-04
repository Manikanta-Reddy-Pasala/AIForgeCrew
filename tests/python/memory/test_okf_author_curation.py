"""Sorting the memory graph out after the fact, and the small record_* cards.

Global scope used to be a default rather than something earned, and the folder
filled with learnings that named one repo's files. Triage fixes that after the
fact: a model decides keep / move-to-a-repo / noise, and because a local model
reliably spots noise but rarely maps to a repo, a deterministic repo-name match
runs behind it — so an unmapped learning still finds its owner.

Two things are deliberate here. The batches are small (~8), because one big
JSON blob made the model skip every project mapping. And "delete" moves the
file into ``okf/.trash/`` and marks a tombstone: reversible on this machine,
and expressible to the mesh — without the tombstone the next pull from a peer
re-plants the node that was just called noise.
"""
from __future__ import annotations

import types as pytypes

import pytest

from aiforge_core.memory.okf import author as A


def _learning(nid, category="", body="", path="/okf/global/x.md", **meta):
    return {"id": nid, "type": "learning", "path": path,
            "body": body, "meta": {"category": category, **meta}}


def _dec(nid, decision="global", repo=""):
    return pytypes.SimpleNamespace(id=nid, decision=decision, repo=repo)


# ─── the deterministic repo-name assist ────────────────────────────────


def test_a_repo_named_in_the_learning_owns_it():
    node = _learning("L1", body="the aiforgecrew api boots in one process")
    assert A._repo_name_match(node, {"AIForgeCrew", "PosFrontend"}) \
        == "AIForgeCrew"


def test_the_category_counts_as_well_as_the_body():
    node = _learning("L1", category="PosFrontend build")
    assert A._repo_name_match(node, {"PosFrontend"}) == "PosFrontend"


def test_a_hyphen_does_not_hide_the_name():
    node = _learning("L1", body="in pos-frontend the vite port is 5174")
    assert A._repo_name_match(node, {"posfrontend"}) == "posfrontend"


def test_a_short_name_cannot_false_hit():
    """'Cache' alone would match half the corpus."""
    node = _learning("L1", body="the cache is warmed at boot")
    assert A._repo_name_match(node, {"Cache"}) == ""


def test_the_longest_matching_name_wins():
    node = _learning("L1", body="see posfrontendadmin for the routes")
    assert A._repo_name_match(node, {"posfrontend", "posfrontendadmin"}) \
        == "posfrontendadmin"


def test_a_learning_naming_nothing_stays_unmapped():
    assert A._repo_name_match(_learning("L1", body="always run tests"),
                              {"AIForgeCrew"}) == ""


# ─── the plan ──────────────────────────────────────────────────────────


def _plan(nodes, decisions, repos={"AIForgeCrew"}):
    by_id = {n["id"]: n for n in nodes}
    return A._reclassify_plan(by_id, {d.id: d for d in decisions}, repos)


def test_a_real_global_rule_is_kept():
    plan = _plan([_learning("L1", body="always run tests")], [_dec("L1")])
    assert plan["keep"] == ["L1"]
    assert not plan["move"]
    assert not plan["delete"]


def test_a_project_decision_moves_it_to_that_repo():
    plan = _plan([_learning("L1")], [_dec("L1", "project", "AIForgeCrew")])
    assert plan["move"] == [("L1", "AIForgeCrew")]


def test_a_repo_the_whitelist_does_not_know_is_not_trusted():
    """The model can name anything; only the known-repo list decides."""
    plan = _plan([_learning("L1")], [_dec("L1", "project", "InventedRepo")])
    assert plan["keep"] == ["L1"]
    assert not plan["move"]


def test_the_name_match_rescues_a_learning_the_model_left_global():
    """A local model marks noise well but rarely maps to a repo."""
    plan = _plan([_learning("L1", body="aiforgecrew boots in one process")],
                 [_dec("L1", "global")])
    assert plan["move"] == [("L1", "AIForgeCrew")]


def test_noise_is_deleted_without_a_name_rescue():
    plan = _plan([_learning("L1", body="aiforgecrew test artifact")],
                 [_dec("L1", "noise")])
    assert plan["delete"] == ["L1"]
    assert not plan["move"]


def test_a_node_the_model_never_ruled_on_still_gets_the_name_match():
    plan = _plan([_learning("L1", body="aiforgecrew ports")], [])
    assert plan["move"] == [("L1", "AIForgeCrew")]


# ─── batching the model call ───────────────────────────────────────────


@pytest.fixture
def triage(monkeypatch):
    """The structured LLM call, per batch."""
    from aiforge_core.llm import structured
    state: dict = {"batches": [], "fail_batch": None}

    def _complete(role, convo, model, **kw):
        idx = len(state["batches"])
        state["batches"].append(convo[1]["content"])
        state["kw"] = kw
        if state["fail_batch"] == idx:
            raise RuntimeError("model down")
        return model(decisions=[{"id": "L1", "decision": "noise"}])
    monkeypatch.setattr(structured, "structured_complete", _complete)
    return state


def test_the_catalogue_is_triaged_in_small_batches(triage):
    """A local model reasons far better over ~8 items than 40 — one big blob
    made it skip every project mapping."""
    items = [{"id": f"L{i}", "category": "", "text": "x"} for i in range(20)]
    A._reclassify_decisions(items, "AIForgeCrew")
    assert len(triage["batches"]) == 3
    assert triage["batches"][0].startswith("REPOS: AIForgeCrew")


def test_a_bad_batch_only_loses_its_own_items(triage):
    triage["fail_batch"] = 0
    items = [{"id": f"L{i}"} for i in range(16)]
    out = A._reclassify_decisions(items, "r")
    assert len(triage["batches"]) == 2
    assert len(out) == 1


def test_the_triage_runs_cold(triage):
    A._reclassify_decisions([{"id": "L1"}], "r")
    assert triage["kw"]["temperature"] == 0.0


# ─── applying the plan ─────────────────────────────────────────────────


@pytest.fixture
def store(monkeypatch, tmp_path):
    from aiforge_core.memory.okf import store as st
    state: dict = {"saved": [], "nodes": [], "scopes": ["AIForgeCrew"],
                   "ok": True, "root": tmp_path}
    monkeypatch.setattr(st, "okf_root", lambda: str(tmp_path))
    monkeypatch.setattr(st, "load_all",
                        lambda scope=None: [n for n in state["nodes"]
                                            if scope is None
                                            or (n.get("meta") or {}).get(
                                                "workspace", "global") == scope
                                            or scope == "global"
                                            and (n.get("meta") or {}).get(
                                                "scope") == "global"])
    monkeypatch.setattr(st, "okr_scopes", lambda: state["scopes"])
    monkeypatch.setattr(st, "_invalidate", lambda: state.update(invalidated=True))
    monkeypatch.setattr(st, "_write_index", lambda: state.update(indexed=True))

    def _save(node_type, node_id, meta, body, reindex=True):
        state["saved"].append({"type": node_type, "id": node_id, "meta": meta,
                               "body": body})
        return {"ok": state["ok"], "id": node_id or f"N{len(state['saved'])}"}
    monkeypatch.setattr(st, "save_node", _save)
    return state


def test_a_moved_learning_is_rescoped_to_its_repo(store):
    by_id = {"L1": _learning("L1", body="b")}
    assert A._apply_moves([("L1", "AIForgeCrew")], by_id) == 1
    meta = store["saved"][0]["meta"]
    assert meta["scope"] == "repo:AIForgeCrew"
    assert meta["workspace"] == "AIForgeCrew", "this is what files it"


def test_a_failed_save_is_not_counted_as_moved(store):
    store["ok"] = False
    assert A._apply_moves([("L1", "r")], {"L1": _learning("L1")}) == 0


def test_noise_is_moved_to_trash_not_unlinked(store, tmp_path, monkeypatch):
    """A mis-classified learning must be restorable on this machine."""
    from aiforge_core.memory.sync import tombstone
    marked: list = []
    monkeypatch.setattr(tombstone, "mark_deleted",
                        lambda origin, nid, rev: marked.append((origin, nid, rev)))
    src = tmp_path / "L1.md"
    src.write_text("body")
    node = _learning("L1", path=str(src), origin="nuc", rev=3)
    assert A._trash_noise(["L1"], {"L1": node}) == 1
    assert (tmp_path / ".trash" / "L1.md").read_text() == "body"
    assert not src.exists()


def test_the_removal_is_expressible_to_the_mesh(store, tmp_path, monkeypatch):
    """Without the tombstone the next pull from a peer re-plants it."""
    from aiforge_core.memory.sync import tombstone
    marked: list = []
    monkeypatch.setattr(tombstone, "mark_deleted",
                        lambda origin, nid, rev: marked.append((origin, nid, rev)))
    src = tmp_path / "L1.md"
    src.write_text("b")
    A._trash_noise(["L1"], {"L1": _learning("L1", path=str(src), origin="nuc",
                                            rev=7)})
    assert marked == [("nuc", "L1", 7)]


def test_a_file_that_will_not_move_is_skipped(store, tmp_path, monkeypatch):
    from aiforge_core.memory.sync import tombstone
    monkeypatch.setattr(tombstone, "mark_deleted", lambda *a: None)
    node = _learning("L1", path=str(tmp_path / "gone.md"))
    assert A._trash_noise(["L1"], {"L1": node}) == 0


# ─── the whole triage ──────────────────────────────────────────────────


@pytest.fixture
def curate(store, monkeypatch):
    state: dict = {"decisions": []}
    monkeypatch.setattr(A, "_reclassify_decisions",
                        lambda items, repos: (_ for _ in ()).throw(
                            state["decisions"])
                        if isinstance(state["decisions"], Exception)
                        else state["decisions"])
    monkeypatch.setattr(A, "_trash_noise", lambda ids, by_id: len(ids))
    return state


def test_an_empty_global_folder_needs_no_model(curate, store):
    assert A.reclassify_global_learnings(["r"]) == {
        "ok": True, "moved": 0, "deleted": 0, "kept": 0,
        "note": "no global learnings"}


def test_the_plan_is_carried_out(curate, store):
    store["nodes"] = [_learning("L1", scope="global"),
                      _learning("L2", scope="global")]
    curate["decisions"] = [_dec("L1", "project", "AIForgeCrew"),
                           _dec("L2", "noise")]
    res = A.reclassify_global_learnings(["AIForgeCrew"])
    assert res["moved"] == 1
    assert res["deleted_to_trash"] == 1
    assert store["invalidated"] is True, "moved files → stale parse cache"
    assert store["indexed"] is True


def test_a_dry_run_touches_nothing(curate, store):
    store["nodes"] = [_learning("L1", scope="global")]
    curate["decisions"] = [_dec("L1", "project", "AIForgeCrew")]
    res = A.reclassify_global_learnings(["AIForgeCrew"], dry_run=True)
    assert res["dry_run"] is True
    assert res["move"] == [("L1", "AIForgeCrew")]
    assert store["saved"] == []
    assert "indexed" not in store


def test_no_model_leaves_the_folder_alone(curate, store):
    store["nodes"] = [_learning("L1", scope="global")]
    curate["decisions"] = ImportError("no llm")
    res = A.reclassify_global_learnings(["r"])
    assert res["ok"] is False
    assert "no llm" in res["error"]
    assert store["saved"] == []


def test_a_decision_for_an_unknown_node_is_ignored(curate, store):
    store["nodes"] = [_learning("L1", scope="global")]
    curate["decisions"] = [_dec("GHOST", "noise")]
    assert A.reclassify_global_learnings(["r"])["kept"] == 1


# ─── the repo card ─────────────────────────────────────────────────────


def test_a_repo_card_is_one_node_per_workspace(store):
    A.record_repo_profile("AIForgeCrew", stack="python", build="uv sync")
    saved = store["saved"][0]
    assert saved["id"] == "R-aiforgecrew"
    assert saved["type"] == "repo"
    assert saved["meta"]["scope"] == "repo:AIForgeCrew"
    assert saved["meta"]["build"] == "uv sync"


def test_a_workspace_is_required(store):
    assert A.record_repo_profile("  ") == {"ok": False, "error": "no workspace"}
    assert store["saved"] == []


def test_scalars_overwrite_and_blanks_are_left_alone(store):
    store["nodes"] = [{"id": "R-r", "type": "repo",
                       "meta": {"workspace": "r", "build": "make",
                                "test": "make test"}, "body": "old"}]
    A.record_repo_profile("r", build="ninja", test="")
    meta = store["saved"][0]["meta"]
    assert meta["build"] == "ninja"
    assert meta["test"] == "make test"


def test_list_fields_accrete_instead_of_churning(store):
    store["nodes"] = [{"id": "R-r", "type": "repo",
                       "meta": {"workspace": "r", "gotchas": ["a"]},
                       "body": "old"}]
    A.record_repo_profile("r", gotchas=["a", "b"])
    assert store["saved"][0]["meta"]["gotchas"] == ["a", "b"]


def test_a_list_field_is_capped():
    meta: dict = {}
    A._union_into(meta, "gotchas", [str(i) for i in range(50)])
    assert len(meta["gotchas"]) == 30


def test_an_empty_body_keeps_what_the_card_had(store):
    store["nodes"] = [{"id": "R-r", "type": "repo", "meta": {"workspace": "r"},
                       "body": "the old body"}]
    A.record_repo_profile("r", body="  ")
    assert store["saved"][0]["body"] == "the old body"


def test_a_broken_store_does_not_raise(store, monkeypatch):
    from aiforge_core.memory.okf import store as st
    monkeypatch.setattr(st, "load_all",
                        lambda scope=None: (_ for _ in ()).throw(OSError("io")))
    assert A.record_repo_profile("r")["ok"] is False


# ─── scripts and tasks ─────────────────────────────────────────────────


def test_a_script_is_recorded_with_how_to_run_it(store):
    r = A.record_script(name="deploy.sh", lang="bash", purpose="ship it",
                        path="tools/deploy.sh", run="./tools/deploy.sh",
                        workspace="r")
    assert r["ok"] is True
    meta = store["saved"][0]["meta"]
    assert meta["lang"] == "shell"
    assert meta["run"] == "./tools/deploy.sh"
    assert meta["title"] == "deploy.sh (shell)"


def test_a_python_script_is_labelled_python(store):
    A.record_script(name="x", lang="Python 3.12")
    assert store["saved"][0]["meta"]["lang"] == "python"


def test_a_script_already_recorded_is_not_duplicated(store):
    store["nodes"] = [{"id": "S1", "type": "script",
                       "meta": {"workspace": "r", "name": "deploy.sh"}}]
    assert A.record_script(name="deploy.sh", lang="sh", workspace="r") == {
        "ok": True, "id": "S1", "deduped": True}
    assert store["saved"] == []


def test_a_nameless_script_is_refused(store):
    assert A.record_script(name=" ", lang="sh")["error"] == "no name"


def test_a_task_recipe_is_recorded(store):
    A.record_task(title="Add a migration", workspace="r", tags=["db"],
                  body="1. write it")
    saved = store["saved"][0]
    assert saved["type"] == "task"
    assert saved["meta"]["tags"] == ["db"]
    assert saved["body"] == "1. write it"


def test_a_task_is_deduped_on_its_normalised_title(store):
    store["nodes"] = [{"id": "T1", "type": "task",
                       "meta": {"workspace": "r", "title": "Add A  Migration"}}]
    assert A.record_task(title="add a migration", workspace="r")["deduped"] \
        is True


def test_a_titleless_task_is_refused(store):
    assert A.record_task(title="")["error"] == "no title"


def test_a_task_body_falls_back_to_its_title(store):
    A.record_task(title="Do the thing")
    assert store["saved"][0]["body"] == "Do the thing"


def test_a_broken_save_is_soft(store, monkeypatch):
    from aiforge_core.memory.okf import store as st
    monkeypatch.setattr(st, "save_node",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("io")))
    assert A.record_task(title="x")["ok"] is False
    assert A.record_script(name="x", lang="sh")["ok"] is False


# ─── the auto-built repo cards ─────────────────────────────────────────


def test_facts_are_bucketed_by_their_category():
    buckets = A._fact_buckets([
        _learning("L1", category="Build", body="- run make"),
        _learning("L2", category="build", body="use ninja"),
        _learning("L3", body="no category")])
    assert buckets["build"] == ["run make", "use ninja"]
    assert buckets["notes"] == ["no category"]


@pytest.mark.parametrize("category", ["structure", "architecture", "layout"])
def test_only_a_genuine_structure_note_is_lifted(category):
    assert A._structure_note({category: ["src/ holds the app"]}) \
        == "src/ holds the app"


def test_a_command_is_never_guessed_from_a_fact():
    """'sync retries…' is a fact, not a test command."""
    assert A._structure_note({"testing": ["sync retries three times"]}) == ""


def test_each_project_gets_a_card_built_from_its_learnings(store, monkeypatch):
    store["scopes"] = ["repoA", "repoB"]
    store["nodes"] = [_learning("L1", category="structure",
                                body="src/ holds it", workspace="repoA")]
    calls: list = []
    monkeypatch.setattr(A, "record_repo_profile",
                        lambda ws, **kw: calls.append((ws, kw)) or {"ok": True})
    assert A.build_repo_profiles() == {"ok": True, "profiles": 1}
    assert [c[0] for c in calls] == ["repoA"], "repoB has nothing to build from"
    assert calls[0][1]["structure"] == "src/ holds it"
    assert calls[0][1]["gotchas"] == ["src/ holds it"]


def test_a_project_with_no_learnings_gets_no_card(store, monkeypatch):
    store["scopes"] = ["repoA"]
    store["nodes"] = []
    monkeypatch.setattr(A, "record_repo_profile",
                        lambda ws, **kw: pytest.fail("built an empty card"))
    assert A.build_repo_profiles()["profiles"] == 0


# ─── seeding the graph from the old flat briefs ────────────────────────


@pytest.fixture
def briefs(monkeypatch, tmp_path):
    from aiforge_core.memory import md_store
    state: dict = {"files": []}
    monkeypatch.setattr(md_store, "iter_briefs", lambda: state["files"])
    return state


def _brief(tmp_path, name, facts, kind="knowledge"):
    p = tmp_path / f"compacted-{name}.md"
    p.write_text(f"---\nkind: {kind}\n---\n\n## Facts\n"
                 + "\n".join(f"- {f}" for f in facts) + "\n")
    return p


def test_a_topics_facts_become_one_global_learning(store, briefs, tmp_path,
                                                   monkeypatch):
    from aiforge_core.memory.okf import graph
    monkeypatch.setattr(graph, "build",
                        lambda force=False: pytypes.SimpleNamespace(nodes={}))
    briefs["files"] = [_brief(tmp_path, "deploy", ["ssh nuc first"])]
    assert A.migrate_from_briefs() == {"ok": True, "migrated": 1, "topics": 1}
    saved = store["saved"][0]
    assert saved["meta"]["scope"] == "global", \
        "the LLM classify step sorts these into projects afterwards"
    assert saved["meta"]["category"] == "deploy"
    assert "ssh nuc" in saved["body"]


def test_a_topic_already_in_the_graph_is_skipped(store, briefs, tmp_path,
                                                 monkeypatch):
    from aiforge_core.memory.okf import graph
    monkeypatch.setattr(graph, "build", lambda force=False:
                        pytypes.SimpleNamespace(nodes={
                            "L1": {"type": "learning",
                                   "meta": {"category": "Deploy"}}}))
    briefs["files"] = [_brief(tmp_path, "deploy", ["x"])]
    assert A.migrate_from_briefs()["migrated"] == 0


def test_the_split_parts_of_one_brief_fold_back_together(briefs, tmp_path):
    briefs["files"] = [_brief(tmp_path, "deploy-1", ["a"]),
                       _brief(tmp_path, "deploy-2", ["b"])]
    assert A._brief_facts_by_topic() == {"deploy": ["a", "b"]}


def test_a_fact_is_flattened_to_one_line(briefs, tmp_path):
    """Read back a line at a time, a multi-line fact would never match itself:
    it looks new every cycle, bumps rev, and re-triggers the admin's fold."""
    p = tmp_path / "compacted-deploy.md"
    p.write_text("---\nkind: knowledge\n---\n\n## Facts\n- ssh   nuc first\n")
    briefs["files"] = [p]
    assert A._brief_facts_by_topic() == {"deploy": ["ssh nuc first"]}


def test_the_same_fact_twice_is_stored_once(briefs, tmp_path):
    briefs["files"] = [_brief(tmp_path, "deploy-1", ["a"]),
                       _brief(tmp_path, "deploy-2", ["a"])]
    assert A._brief_facts_by_topic() == {"deploy": ["a"]}


def test_a_non_knowledge_brief_is_not_migrated(briefs, tmp_path):
    briefs["files"] = [_brief(tmp_path, "deploy", ["a"], kind="session")]
    assert A._brief_facts_by_topic() == {}


def test_an_unreadable_brief_is_skipped(briefs, tmp_path):
    briefs["files"] = [tmp_path / "compacted-gone.md",
                       _brief(tmp_path, "ok", ["a"])]
    assert A._brief_facts_by_topic() == {"ok": ["a"]}
