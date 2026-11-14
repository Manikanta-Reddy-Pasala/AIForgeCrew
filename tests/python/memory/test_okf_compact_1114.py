"""No-model compaction gaps found in the 11-14 dry run on a copy of nuc memory:
markdown twins and cross-section repeats survived scrub_items, run narration
and toy-task topics were indexed as knowledge, and the vec0 index kept rows
for units long deleted."""
from __future__ import annotations

import json
import sqlite3

import pytest

from aiforge_core.runtime import work_notes
from aiforge_core.runtime.work_notes import item_key, scrub_items, scrub_sections
from aiforge_core.memory.md_store._prune_classify import is_narration, is_toy, off_topic


# ── 1. duplicates ─────────────────────────────────────────────────────────

def test_markdown_twins_are_one_fact():
    a = "The `mask_email(email)` function masks the **local** part"
    b = "The mask_email(email) function masks the local part"
    c = "[topic] The [mask_email(email)](http://x/y) function masks the local part."
    assert item_key(a) == item_key(b) == item_key(c)
    assert scrub_items([a, b, c]) == [a]


def test_articles_do_not_make_a_new_fact():
    assert item_key("SPEC.md contains the requirements") == item_key(
        "SPEC.md contains requirements")


def test_strict_off_restores_old_keys(monkeypatch):
    monkeypatch.setenv("AIFORGE_SCRUB_STRICT", "0")
    assert item_key("`x` y") != item_key("x y")


def test_a_fact_in_two_sections_stays_in_the_most_specific_one():
    kr, facts, lrn = scrub_sections(
        key_results=["uses `redis`", "measures 3 repos"],
        facts=["Uses redis."],
        learnings=["uses redis", "retry needs WG up", "measures 3 repos"])
    assert facts == ["Uses redis."]
    assert lrn == ["retry needs WG up", "measures 3 repos"]
    assert kr == []


def test_a_joined_line_whose_parts_exist_is_split():
    a, b, c = "User has id and email", "Session has a token", "is_x checks y"
    out = scrub_items([a, b, f"{a} - {b} - {c}"])
    assert out == [a, b, c]


def test_a_joined_line_with_new_parts_is_left_alone():
    s = "build - then test - then deploy"
    assert scrub_items(["other fact", s]) == ["other fact", s]


def test_render_drops_cross_section_repeats():
    text = work_notes.render_note("knowledge", "k", title="k",
                                  key_results=["`a` b"], facts=["a b"])
    sec = work_notes.parse_note(text)["sections"]
    assert sec["facts"] == ["a b"] and not sec.get("key_results")


# ── 2/3. classifiers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("s", [
    "Integrated with CMake and ctest — pipeline complete, 4/4 subtasks built",
    "Created calc.py with def add(a, b): return a + b",
    "Created test_todo.py with 6 pytest tests covering all flows (all passing)",
    "The mask_email(email) function was added to /tmp/p/app/util/helpers.py",
    "Added 10 new test cases; all 20 tests pass",
    "[task-history] DID: write a function",
    "Tools used: file_write, run_command",
    "Current working directory is /home/ai/.aiforge/repos/t3-a/.worktrees/p",
    "On 2026-07-17 at 21:34, a user submitted a request labeled 'x'",
])
def test_narration_is_recognised(s):
    assert is_narration(s)


@pytest.mark.parametrize("s", [
    "MessageRetryService polls pendingToSync every 30 seconds with batch size 50",
    "Tests pass only when NATS is running locally",
    "json_util.py must create parent directories when dumping to a path",
    "The user asked that commits are never automatic",
    "Install with `pip install -e .`, then `pytest -q` to run tests",
])
def test_knowledge_is_not_narration(s):
    assert not is_narration(s)


def test_toy_tasks():
    assert is_toy("Implement is_prime(n) with 3 asserts")
    assert is_toy("TodoStore must support add(), list()")
    assert not is_toy("NATS JetStream streams use subject pendingToSync")


def test_off_topic_moves_only_unconnected_facts():
    items = ["AiForgeMemory parses source with Tree-sitter",
             "AiForgeMemory stores symbols in Neo4j",
             "Neo4j queries back the dashboard search",
             "godaddy-webhook solves ACME challenges",
             "godaddy-webhook lacks retry logic",
             "AiForgeMemory dashboard uses FastAPI",
             "FastAPI serves the read-only dashboard endpoints"]
    assert off_topic("aiforgememory", items) == [3, 4]


def test_off_topic_keeps_rules_short_items_and_bucket_topics():
    on = ["redis cache keys expire hourly", "redis cluster has three shards",
          "redis sentinel watches the shards", "redis backups run nightly",
          "redis memory limit is eight gigabytes"]
    assert off_topic("redis", on + ["MAX_CONCURRENT_MESSAGES=4",
                                    "CONSTRAINT: keep godaddy webhook verbatim",
                                    "Deploys must wait for kafka topics"]) == []
    assert off_topic("redis", on + ["godaddy webhook solves acme"]) == [5]
    assert off_topic("shared-2-2", on + ["godaddy webhook solves acme"]) == []


def test_off_topic_is_a_no_op_when_no_fact_names_the_topic():
    assert off_topic("PosClientBackend", ["a b c alpha", "beta gamma delta",
                                          "epsilon zeta", "theta iota"]) == []


def test_off_topic_never_moves_half_the_brief():
    items = ["redis cache keys", "godaddy webhook", "kafka topics", "nginx proxy"]
    assert off_topic("redis", items) == []


# ── 2/3. the passes ───────────────────────────────────────────────────────

@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    return tmp_path


def _brief(topic, facts, learnings=()):
    from aiforge_core.memory import md_store
    md_store.briefs_dir().mkdir(parents=True, exist_ok=True)
    body = "".join(f"- {f}\n" for f in facts)
    lrn = ("\n## Learnings\n\n" + "".join(f"- {x}\n" for x in learnings)) if learnings else ""
    p = md_store.briefs_dir() / f"compacted-{topic}.md"
    p.write_text(
        f"---\ntype: knowledge\nkind: knowledge\nkey: {topic}\nresource: ''\n"
        f"timestamp: 2026-08-18T00:00:00Z\ntags: []\nlinks: []\n---\n\n"
        f"# {topic}\n\n## Facts\n\n{body}{lrn}", encoding="utf-8")
    return p


def _moved(mem):
    recs = []
    for f in (mem / "mem" / "archive").glob("*/moved-facts.jsonl"):
        recs += [json.loads(x) for x in f.read_text().splitlines()]
    return recs


def test_narration_moves_to_the_archive_record(mem):
    from aiforge_core.memory import md_store
    p = _brief("nats", ["NATS stream pendingToSync holds pushes",
                        "Built — all tests pass"],
               learnings=["Created nats_sync.py with retry"])
    out = md_store.prune_narration()
    assert out["brief_bullets"] == 2
    sec = work_notes.parse_note(p.read_text())["sections"]
    assert sec["facts"] == ["NATS stream pendingToSync holds pushes"]
    assert not sec.get("learnings")
    assert {r["text"] for r in _moved(mem)} == {"Built — all tests pass",
                                                "Created nats_sync.py with retry"}


def test_narration_switch(mem, monkeypatch):
    from aiforge_core.memory import md_store
    monkeypatch.setenv("AIFORGE_COMPACT_NARRATION", "0")
    assert md_store.prune_narration()["skipped"] == "disabled"


def test_toy_topic_is_archived_whole_and_real_topic_kept(mem):
    from aiforge_core.memory import md_store
    toy = _brief("calc", ["Created calc.py with def add(a, b)",
                          "Added subtract(a, b) with 5 tests; all 10 tests pass"])
    real = _brief("bug", ["Built — all tests pass",
                          "PosClientBackend pushes sync data to NATS"])
    out = md_store.sweep_junk_topics()
    assert out["briefs_archived"] == 1
    assert not toy.exists() and real.exists()
    archived = list((mem / "mem" / "archive").glob("*/compacted/compacted-calc.md"))
    assert len(archived) == 1
    assert len([r for r in _moved(mem) if r["reason"] == "junk-topic"]) == 3


def test_off_topic_facts_leave_a_real_topic(mem):
    from aiforge_core.memory import md_store
    p = _brief("aiforgememory", [
        "AiForgeMemory parses source with Tree-sitter",
        "AiForgeMemory stores symbols in Neo4j",
        "Neo4j queries back the dashboard search",
        "godaddy-webhook solves ACME challenges on GoDaddy domains",
        "AiForgeMemory dashboard uses FastAPI",
        "FastAPI serves the read-only dashboard endpoints"])
    out = md_store.sweep_junk_topics()
    assert out["off_topic_facts"] == 1
    facts = work_notes.parse_note(p.read_text())["sections"]["facts"]
    assert len(facts) == 5 and not any("godaddy" in f for f in facts)
    assert [r["reason"] for r in _moved(mem)] == ["off-topic"]


def test_junk_okf_node_is_archived(mem):
    from aiforge_core.memory.okf import store
    from aiforge_core.memory import md_store
    store.save_node("learning", None, {"scope": "global", "category": "hello",
                                       "title": "hello", "tags": ["topic:hello"]},
                    "- Created hello.py with add(a, b)\n- Tools used: file_write")
    store.save_node("learning", None, {"scope": "global", "category": "nats",
                                       "title": "nats", "tags": ["topic:nats"]},
                    "- NATS stream pendingToSync holds pushes\n- Created x.py, 3 tests pass")
    out = md_store.sweep_junk_topics()
    assert out["nodes_archived"] == 1
    left = [d for d in store.load_all() if d.get("type") == "learning"]
    assert [d["meta"]["category"] for d in left] == ["nats"]
    md_store.prune_narration()
    left = [d for d in store.load_all() if d.get("type") == "learning"]
    assert left[0]["body"].strip() == "- NATS stream pendingToSync holds pushes"


# ── 4. vec orphans ────────────────────────────────────────────────────────

def test_vec_orphans_are_removed(mem):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from aiforge_core.memory.sqlite_memory import prune_vec_orphans
    db = str(mem / "m.db")
    c = sqlite3.connect(db)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.execute("CREATE TABLE memory_units (id INTEGER PRIMARY KEY, text TEXT)")
    c.execute("CREATE VIRTUAL TABLE vec_memory USING vec0(embedding float[2])")
    c.execute("INSERT INTO memory_units(id, text) VALUES (1, 'a')")
    for i in (1, 2, 3):
        c.execute("INSERT INTO vec_memory(rowid, embedding) VALUES (?, ?)",
                  (i, json.dumps([0.1, 0.2])))
    c.commit()
    c.close()
    out = prune_vec_orphans()
    assert out["removed"] == 2 and out["vacuum"] == "done"
    c = sqlite3.connect(db)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    assert [r[0] for r in c.execute("SELECT rowid FROM vec_memory")] == [1]
    assert prune_vec_orphans()["removed"] == 0


def test_vec_prune_switch_and_missing_table(mem, monkeypatch):
    from aiforge_core.memory.sqlite_memory import prune_vec_orphans
    sqlite3.connect(str(mem / "m.db")).execute("CREATE TABLE t (x)").connection.close()
    assert prune_vec_orphans()["skipped"] == "no vec table"
    monkeypatch.setenv("AIFORGE_COMPACT_VEC_PRUNE", "0")
    assert prune_vec_orphans()["skipped"] == "disabled"
