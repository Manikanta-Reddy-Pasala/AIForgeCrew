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
