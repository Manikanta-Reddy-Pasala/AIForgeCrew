"""OKF v0.1 conformance helpers + injection into compaction/learner."""
import os
import tempfile

from aiforge_core.memory import okf


def test_frontmatter_has_type_and_recommended_fields_in_order():
    fm = okf.okf_frontmatter("learning", title="T", description="D",
                             tags=["a", "b"], timestamp="2026-07-11T00:00:00Z",
                             extra={"linked_krs": ["KR-1"]})
    assert fm.startswith("---\ntype: learning\n")
    # recommended fields in priority order
    order = [fm.index("title:"), fm.index("description:"),
             fm.index("tags:"), fm.index("timestamp:")]
    assert order == sorted(order)
    assert "linked_krs: [KR-1]" in fm       # custom key preserved


def test_validate_file():
    assert okf.validate_file("---\ntype: x\n---\nbody") == []
    assert okf.validate_file("no frontmatter")            # missing block
    assert okf.validate_file("---\ntitle: x\n---\n")      # no type
    assert okf.validate_file("# index only", is_reserved=True) == []


def test_append_log_newest_first_iso():
    p = os.path.join(tempfile.mkdtemp(), "log.md")
    okf.append_log(p, "old", date="2026-07-10")
    okf.append_log(p, "new", date="2026-07-11")
    okf.append_log(p, "new2", date="2026-07-11")
    txt = open(p).read()
    assert txt.index("## 2026-07-11") < txt.index("## 2026-07-10")   # newest first
    assert "- new2" in txt and "- new" in txt


def test_okf_rules_injected_into_producers():
    from aiforge_core.runtime import work_notes as wn
    from aiforge_core.runtime.prompts import learner
    assert "OPEN KNOWLEDGE FORMAT" in wn._CONSOLIDATE_SYS
    assert "Open Knowledge Format" in learner.PROMPT


def test_okr_store_writes_reserved_index_without_frontmatter(monkeypatch):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", tempfile.mkdtemp())
    from aiforge_core.memory.okr import store
    store.save_node("objective", None, {"title": "O"}, "body")
    idx = os.path.join(store.okr_root(), "index.md")
    assert os.path.isfile(idx)
    assert not open(idx).read().startswith("---")       # reserved: NO frontmatter
    assert "/objectives/" in open(idx).read()           # absolute bundle link


def test_record_solution_okf_node_log_and_dedup(monkeypatch, tmp_path):
    """A completed feature/fix → an OKF `solution` node (kind + workspace +
    topic + tables + services) + a dated log.md entry; a duplicate is skipped."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    from aiforge_core.memory.okr import author, store
    r = author.record_solution(
        kind="fix", summary="MessageRetryService reads priority header",
        workspace="PosClientBackend", topic="sync",
        tables=["productTxn"], services=["NATS", "PosServerBackend"],
        about=["MessageRetryService.java"], ticket="ONE-9", date="2026-07-11")
    assert r["ok"]
    node = open(r["path"]).read()
    for want in ("type: solution", 'kind: "fix"', "PosClientBackend",
                 "sync", "productTxn", "NATS", "PosServerBackend"):
        assert want in node, want
    import os as _os
    log = open(_os.path.join(store.okr_root(), "log.md")).read()
    assert "## 2026-07-11" in log and "[fix]" in log
    # dedup: same fix again → no second node
    author.record_solution(kind="fix", summary="did: MessageRetryService reads priority header",
                           workspace="PosClientBackend", ticket="ONE-9", date="2026-07-11")
    sols = [d for d in store.load_all() if d.get("type") == "solution"]
    assert len(sols) == 1


def test_okr_scope_segregation_global_vs_project(monkeypatch, tmp_path):
    """Solutions/learnings segregate into global/ vs projects/<workspace>/ by
    derived scope; ids stay globally unique; index groups by scope; a legacy
    flat node migrates to its scoped home."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import os
    from aiforge_core.memory.okr import store as s
    # legacy flat node (pre-segregation) with a workspace
    os.makedirs(os.path.join(s.okr_root(), "solutions"))
    open(os.path.join(s.okr_root(), "solutions", "S-01.md"), "w").write(
        '---\ntype: solution\nid: "S-01"\nkind: "fix"\nworkspace: "RepoA"\n---\nx')
    s.save_node("solution", None, {"kind": "feature", "title": "T",
                                   "workspace": "RepoB"}, "b")     # → projects/RepoB
    s.save_node("learning", None, {"scope": "global", "title": "G"}, "g")  # → global
    s.save_node("learning", None, {"scope": "repo:RepoA", "title": "R"}, "r")  # RepoA
    assert s.next_id("solution") == "S-03"       # globally unique (S-01+S-02 taken)
    r = s.migrate_scoped()
    assert r["moved"] == 1                                    # the flat S-01 moved
    assert set(s.okr_scopes()) == {"RepoA", "RepoB"}
    assert (s.read_node("solution", "S-01") or {})["path"].endswith(
        "projects/RepoA/solutions/S-01.md")
    assert sorted(d["id"] for d in s.load_all("global")) == ["L-01"]
    assert sorted(d["id"] for d in s.load_all("RepoA")) == ["L-02", "S-01"]
    idx = open(os.path.join(s.okr_root(), "index.md")).read()
    assert "## Global" in idx and "## RepoA" in idx and "## RepoB" in idx
    assert not idx.startswith("---")                          # reserved: no frontmatter


def test_okr_learning_classification_global_vs_repo(monkeypatch, tmp_path):
    """extract_and_save classifies each learning: scope 'repo' → projects/<repo>/
    with a topic category; scope 'global' → global/."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    from aiforge_core.memory.okr import author, store

    class _L:
        def __init__(s, rule, scope, topic=""):
            s.rule, s.scope, s.topic = rule, scope, topic

    class _R:
        objectives, key_results = [], []
        learnings = [_L("handlers live in src/api", "repo", "structure"),
                     _L("always target one test file", "global", "testing")]
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete",
                        lambda *a, **k: _R())
    author.extract_and_save("a work session long enough to pass the gate here",
                            repo="RepoA")
    assert store.okr_scopes() == ["RepoA"]
    repo_l = store.load_all("RepoA")
    assert repo_l and (repo_l[0]["meta"] or {}).get("category") == "structure"
    glob_l = store.load_all("global")
    assert glob_l and "target one test" in (glob_l[0].get("body") or "")


def test_context_block_scoped_no_cross_project_leak(monkeypatch, tmp_path):
    """Repo context = global rules + THIS repo only; another project's learning
    never leaks in (the 'links all documents' fix)."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import importlib
    from aiforge_core.memory.okr import store
    R = importlib.import_module("aiforge_core.memory.okr.retrieve")
    store.save_node("learning", None, {"scope": "global", "title": "univ rule"}, "g")
    store.save_node("learning", None,
                    {"scope": "repo:RepoA", "workspace": "RepoA"}, "A rule")
    store.save_node("learning", None,
                    {"scope": "repo:RepoB", "workspace": "RepoB"}, "B SECRET rule")
    blk = R.context_block(repo="RepoA")
    assert "univ rule" in blk and "A rule" in blk
    assert "B SECRET" not in blk               # no cross-project leak


def test_okr_load_all_cache_and_deferred_index(monkeypatch, tmp_path):
    """load_all caches the full parse on a dir signature (invalidated by a
    write); reindex=False skips the per-node index rewrite so bulk callers pay
    it once."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import os
    from aiforge_core.memory.okr import store as s
    s.save_node("learning", None, {"scope": "global", "title": "A"}, "a")
    a = s.load_all()
    assert a is not s.load_all()          # returns a fresh list copy…
    assert s._CACHE["all"] is not None    # …but the underlying parse is cached
    # a new write changes the signature → cache refreshes, new node visible
    s.save_node("learning", None, {"scope": "repo:RepoX", "workspace": "RepoX"}, "b")
    assert len(s.load_all()) == 2
    # reindex=False: index NOT rewritten by the save itself
    idx = os.path.join(s.okr_root(), "index.md")
    os.remove(idx)
    s.save_node("learning", None, {"scope": "global", "title": "C"}, "c",
                reindex=False)
    assert not os.path.exists(idx)        # deferred — no index write
    s._write_index()
    assert os.path.exists(idx) and "## RepoX" in open(idx).read()


def test_context_block_query_relevance_returns_related_only(monkeypatch, tmp_path):
    """Read returns only documents RELATED to the query, not the whole scope."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import importlib
    from aiforge_core.memory.okr import store
    R = importlib.import_module("aiforge_core.memory.okr.retrieve")
    for cat, body in [("sync", "sync retries use exponential backoff resilience4j"),
                      ("cache", "cache eviction uses TTL and size limits"),
                      ("auth", "JWT tokens expire after 15 minutes")]:
        store.save_node("learning", None,
                        {"scope": "repo:R", "workspace": "R", "category": cat}, body)
    blk = R.context_block(repo="R", query="fix the cache eviction bug")
    assert "eviction" in blk                      # the related doc surfaces
    assert "resilience4j" not in blk and "JWT" not in blk   # unrelated filtered


def test_context_block_fuzzy_fallback_ladder(monkeypatch, tmp_path):
    """Read ranks EXACT → FUZZY (stem/typo) → recency: a stemmed or misspelled
    query still finds the note; a totally unrelated query falls back non-empty."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import importlib
    from aiforge_core.memory.okr import store
    R = importlib.import_module("aiforge_core.memory.okr.retrieve")
    for cat, body in [("sync", "sync retries use exponential backoff"),
                      ("cache", "cache eviction uses TTL and size limits")]:
        store.save_node("learning", None,
                        {"scope": "repo:R", "workspace": "R", "category": cat}, body)
    assert "eviction" in R.context_block(repo="R", query="evicting from cache")  # stem
    assert "eviction" in R.context_block(repo="R", query="cach evicton bug")     # typo
    assert R.context_block(repo="R", query="kubernetes deploy pipeline")          # fallback non-empty


def test_repo_script_task_nodes_and_retrieval(monkeypatch, tmp_path):
    """repo card upserts (scalars overwrite, lists union); script/task dedup;
    read surfaces the repo hub first, then scripts + task recipes."""
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path))
    import importlib
    from aiforge_core.memory.okr import author, store
    R = importlib.import_module("aiforge_core.memory.okr.retrieve")
    author.record_repo_profile("CacheLayer", stack="Java", build="./mvnw package",
                               gotchas=["L1+L2 together"])
    author.record_repo_profile("CacheLayer", test="./mvnw test",
                               gotchas=["eviction TTL"])            # upsert/merge
    author.record_script(name="reindex.sh", lang="shell", purpose="rebuild FTS",
                         workspace="CacheLayer")
    author.record_script(name="reindex.sh", lang="shell", workspace="CacheLayer")  # dedup
    author.record_task(title="add a cache region", workspace="CacheLayer", body="steps")
    proj = store.load_all("CacheLayer")
    card = next(d for d in proj if d["type"] == "repo")
    assert card["id"] == "R-cachelayer"
    assert card["meta"]["build"] == "./mvnw package" and card["meta"]["test"] == "./mvnw test"
    assert len(card["meta"]["gotchas"]) == 2                       # unioned
    assert len([d for d in proj if d["type"] == "script"]) == 1    # deduped
    blk = R.context_block(repo="CacheLayer", query="how to build the cache")
    assert "Profile:" in blk and "reindex.sh" in blk and "add a cache region" in blk
