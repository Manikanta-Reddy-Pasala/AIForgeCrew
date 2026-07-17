"""End-to-end: populate BOTH memory stores with many files across scopes +
topics (incl. the exact failure shapes the user hit), run the real compaction /
tidy algorithms, and assert the clean end-state:

OKF node store (folder layout):
  - global/<type>/ + projects/<repo>/<type>/ ONLY — no phantom session-<id> scopes
  - one file per concept (paraphrases collapse), ≤2 per concept (global+project)

md_store briefs:
  - NO kind-named junk (compacted-learning.md …)
  - drifted topic slugs collapse to one canonical file
  - no fact duplicated across a project/topic brief and the global brief
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    # deterministic: no LLM topic labelling / summarisation
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    import aiforge_core.memory.okf.store as okf
    import aiforge_core.memory.md_store as md
    importlib.reload(okf)
    return importlib.reload(md), okf


# ── OKF node store: folders + one-concept-one-file ───────────────────────────

def test_okf_folders_and_concept_identity(mem):
    _md, okf = mem
    # same concept, two runs → must stay ONE global file (reuse, not L-01/L-07…)
    for _ in range(3):
        lid = okf.find_by_concept("learning", {"scope": "global"},
                                  "Always paginate jira_search")
        okf.save_node("learning", lid, {"scope": "global"},
                      "Always paginate jira_search")
    # a project-scoped concept (different repo) is its own file — global+project ok
    for repo in ("CacheLayer", "PosPythonBackend"):
        meta = {"workspace": repo, "scope": f"repo:{repo}"}
        lid = okf.find_by_concept("learning", meta, f"Build {repo} with mvnw")
        okf.save_node("learning", lid, meta, f"Build {repo} with mvnw")
    # phantom per-session scopes (the unpinned-chat bug)
    for sid in (64, 65, 66):
        m = {"workspace": f"session-{sid}", "scope": f"repo:session-{sid}"}
        okf.save_node("learning", None, m, "Prefer async IO everywhere")

    assert set(okf.okr_scopes()) >= {"CacheLayer", "PosPythonBackend",
                                     "session-64", "session-65", "session-66"}
    # run the repair algos
    okf.fold_session_scopes_to_global()          # kill phantom session scopes
    okf.dedupe_nodes()                            # collapse any exact dupes

    scopes = set(okf.okr_scopes())
    assert not any(s.startswith("session-") for s in scopes), scopes
    assert scopes == {"CacheLayer", "PosPythonBackend"}          # real projects only
    learns = [d for d in okf.load_all() if d.get("type") == "learning"]
    # jira concept = ONE global file; async-IO folded global = ONE; 2 project = 2
    globals_ = [d for d in learns
                if okf._scope_label_from_path(d["path"]) == "Global"]
    assert len(globals_) == 2                     # jira + folded async-IO (one each)
    assert len(learns) == 4                        # 2 global + 2 project


# ── md_store briefs: names + canonical topics + no cross-scope dup ───────────

def test_md_briefs_clean_after_compaction(mem):
    md, _okf = mem
    # untopic'd notes (would have minted compacted-<kind>.md junk)
    md.capture("learning", "Keep functions under 50 lines", classify=False)
    md.capture("user_comment", "The user prefers terse output", classify=False)
    # drifted topic slugs for ONE subject
    md.capture("topic_learning", "gps fix A", topic="gps", classify=False)
    md.capture("topic_learning", "gps fix B", topic="gpst", classify=False)
    md.capture("topic_learning", "gps fix C", topic="gpst-config", classify=False)
    # a fact that lives BOTH in a repo brief and global (cross-scope dup)
    dup = "Restart the api after every deploy"
    md.capture("project_learning", dup, repo="PosPythonBackend", classify=False)
    md.capture("learning", dup, classify=False)   # → shared/global

    # deterministic compaction (no LLM) then the tidy algo
    md.compact(group_by="topic", min_group=1, summarize=False, archive_sources=False)
    res = md.tidy_briefs()
    assert res["ok"]

    from aiforge_core.memory.md_store import iter_briefs
    from aiforge_core.memory.md_store._graph._reconcile import _CAPTURE_SIG_RE
    stems = [p.stem[len("compacted-"):] for p in iter_briefs()
             if not _CAPTURE_SIG_RE.search(p.name)]
    KIND_JUNK = {"learning", "topic-learning", "user-comment", "rule", "session",
                 "note", "project-learning", "topic-suggestion", "skills",
                 "task-history", "project", "repo"}
    # 1. proper names — no kind-named briefs
    assert not (set(stems) & KIND_JUNK), f"kind-named junk survived: {stems}"
    # 2. gps family collapsed to ONE canonical topic (no gpst / gpst-config)
    gps_like = [s for s in stems if s.startswith("gps")]
    assert len(gps_like) <= 1, f"gps topic drifted: {gps_like}"
    # 3. no fact duplicated across a project brief and global
    from aiforge_core.runtime import work_notes
    from aiforge_core.memory.md_store._base import brief_path
    sp = brief_path("shared")
    if sp.is_file():
        gfacts = {work_notes._ci_key(f if isinstance(f, str) else f.get("text", ""))
                  for f in work_notes.parse_note(
                      sp.read_text())["sections"].get("facts") or []}
        rp = brief_path("PosPythonBackend")
        if rp.is_file():
            rfacts = {work_notes._ci_key(f if isinstance(f, str) else f.get("text", ""))
                      for f in work_notes.parse_note(
                          rp.read_text())["sections"].get("facts") or []}
            assert not (gfacts & rfacts), "fact duplicated across repo + global"
