"""Duplicate and nested-bullet memory (nuc, 11-13): 90 of 249 OKF nodes and 6
briefs held "- - X" lines that grew a level on every rev, briefs kept
"[topic] X" next to "X", and a fold's dedupe was undone at write time."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import work_notes
from aiforge_core.runtime.work_notes import clean_item, item_key, scrub_items


# ── one list item, one form ────────────────────────────────────────────────

def test_stray_bullets_are_stripped_at_any_depth():
    assert clean_item("- - - FIBCACHE avoids recursion") == "FIBCACHE avoids recursion"
    assert clean_item("* • X") == "X"


def test_an_item_joined_with_its_own_copies_is_split_back_to_one():
    x = "FIBCACHE stores entries like `{0: 0}` to prevent redundant calls."
    assert scrub_items([f"- {x} - - {x}", x]) == [x]


def test_a_real_hyphenated_fact_is_left_alone():
    s = "build - then test - then deploy"
    assert scrub_items([s]) == [s]
    assert scrub_items(["1. install", "2) run"]) == ["1. install", "2) run"]


def test_a_topic_tagged_twin_and_case_or_period_variants_are_one_fact():
    out = scrub_items(["mask_email masks the local part.",
                       "[account-number-masking] mask_email masks the local part",
                       "MASK_EMAIL masks the  local part"])
    assert out == ["mask_email masks the local part."]


def test_scaffolding_leaks_are_still_dropped():
    assert scrub_items(["## Facts", "---", "real fact"]) == ["real fact"]


def test_rendered_note_has_one_bullet_per_fact():
    text = work_notes.render_note("knowledge", "k", title="k",
                                  facts=["- - a", "a", "b"])
    facts = work_notes.parse_note(text)["sections"]["facts"]
    assert facts == ["a", "b"]
    assert "- - " not in text


def test_item_key_ignores_trailing_punctuation():
    assert item_key("Uses Redis.") == item_key("uses redis")


# ── OKF nodes: a corrupted node is repaired once, then left alone ─────────

@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "book")
    monkeypatch.delenv("AIFORGE_ADMIN_URL", raising=False)
    return tmp_path


def _brief(topic, facts):
    from aiforge_core.memory import md_store
    body = "".join(f"- {f}\n" for f in facts)
    (md_store.briefs_dir() / f"compacted-{topic}.md").write_text(
        f"---\ntype: knowledge\nkind: knowledge\nkey: {topic}\nresource: ''\n"
        f"timestamp: 2026-08-18T00:00:00Z\ntags: []\nlinks: []\n---\n\n"
        f"# {topic}\n\n## Facts\n\n{body}", encoding="utf-8")


def _node():
    from aiforge_core.memory.okf import store
    return [d for d in store.load_all() if d.get("type") == "learning"][0]


def test_a_nested_bullet_node_is_rewritten_clean_once(mem):
    from aiforge_core.memory.okf import author, store
    _brief("fib", ["FIBCACHE avoids recursion"])
    author.sync_briefs_to_nodes()
    n = _node()
    bad = ("- FIBCACHE avoids recursion\n- - FIBCACHE avoids recursion\n"
           "- - - FIBCACHE avoids recursion - - FIBCACHE avoids recursion")
    store.save_node("learning", n["id"], n.get("meta") or {}, bad, reindex=False)

    out = author.sync_briefs_to_nodes()
    assert out["updated"] == 1
    assert _node()["body"].strip() == "- FIBCACHE avoids recursion"
    assert author.sync_briefs_to_nodes()["updated"] == 0      # converged


def test_a_tagged_twin_in_the_brief_is_not_a_new_fact(mem):
    from aiforge_core.memory.okf import author
    _brief("mask", ["mask_email masks the local part"])
    author.sync_briefs_to_nodes()
    _brief("mask", ["mask_email masks the local part",
                    "[mask] mask_email masks the local part."])
    assert author.sync_briefs_to_nodes()["updated"] == 0


# ── compaction: the fold's dedupe survives the write ──────────────────────

def _prepared(tmp_path, monkeypatch, pre, folded, on_disk):
    from aiforge_core.memory.md_store import _compact as C
    monkeypatch.setattr(C, "_existing_brief_sections",
                        lambda path, base: ({"facts": on_disk}, []))
    monkeypatch.setattr(C, "_brief_parts", lambda *a, **k: [("p", "x")])
    p = {"key": "k", "base_stem": "compacted-k", "tags": [], "title": "t",
         "sources": [], "pre_facts": pre,
         "sections": {"facts": list(folded)}, "parts": []}
    C._late_facts(p)
    return p["sections"]["facts"]


def test_facts_the_fold_merged_away_are_not_put_back(tmp_path, monkeypatch):
    pre = ["uses redis", "Uses Redis cache.", "retries 3x"]
    folded = ["uses a Redis cache", "retries 3x"]
    assert _prepared(tmp_path, monkeypatch, pre, folded, on_disk=pre) == folded


def test_a_fact_captured_during_the_fold_is_kept(tmp_path, monkeypatch):
    pre = ["uses redis"]
    folded = ["uses a Redis cache"]
    out = _prepared(tmp_path, monkeypatch, pre, folded,
                    on_disk=pre + ["deploy needs WG up"])
    assert out == ["uses a Redis cache", "deploy needs WG up"]


def test_no_snapshot_carries_nothing_rather_than_everything(tmp_path, monkeypatch):
    out = _prepared(tmp_path, monkeypatch, None, ["a"], on_disk=["a", "b", "c"])
    assert out == ["a"]


# ── the evening pass corrects what is already on disk ─────────────────────

def test_normalize_cleans_briefs_and_nodes_once(mem):
    from aiforge_core.memory import md_store
    from aiforge_core.memory.okf import author, store
    _brief("fib", ["FIBCACHE avoids recursion"])
    author.sync_briefs_to_nodes()
    n = _node()
    store.save_node("learning", n["id"], n.get("meta") or {},
                    "- X\n- - X\n- - - X - - X", reindex=False)
    p = md_store.briefs_dir() / "compacted-dup.md"
    _brief("dup", ["- uses redis", "uses redis.", "[dup] Uses Redis", "other"])

    out = md_store.normalize_all()
    assert out["briefs"] >= 1 and out["nodes"] >= 1 and out["errors"] == 0
    from aiforge_core.runtime import work_notes
    facts = work_notes.parse_note(p.read_text())["sections"]["facts"]
    assert facts == ["uses redis", "other"]
    assert _node()["body"].strip() == "- X"
    again = md_store.normalize_all()
    assert again["briefs"] == 0 and again["nodes"] == 0     # clean store: no writes


def test_every_recompact_starts_with_normalize(monkeypatch):
    from aiforge_core.memory import _migrations_recompact as R
    from aiforge_core.memory import md_store
    names = []
    monkeypatch.setattr(R, "_run_recompact_steps",
                        lambda steps, out, on_step, cp: names.extend(n for n, _ in steps))
    R.force_recompact_all()
    assert names[0] == "normalize"
