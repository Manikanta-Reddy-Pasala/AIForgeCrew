"""Write-time brief maintenance (audit W1/W2/W6) — the cheap no-LLM fold.

W1 supersede: a new "key: value" fact replaces the stale one at WRITE time
(no stale window until compaction).
W2 tickets: a jira/issue key in a fact is seeded into Key Results.
W6 superset: a new fact that CONTAINS an existing shorter one prunes the short.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def _facts_kr(md_store, repo="svc"):
    from aiforge_core.runtime.work_notes import parse_note
    p = md_store.memory_dir() / f"compacted-{md_store._slug(repo)}.md"
    sec = parse_note(p.read_text())["sections"]
    return sec.get("facts", []), sec.get("key_results", [])


def test_w1_supersede_same_key(mem):
    from aiforge_core.memory import md_store
    md_store._brief_upsert("svc", "status: Open")
    md_store._brief_upsert("svc", "status: Done")
    facts, _ = _facts_kr(md_store)
    assert any("status: Done" in f for f in facts)
    assert not any("status: Open" in f for f in facts)


def test_w2_ticket_seeded_into_key_results(mem):
    from aiforge_core.memory import md_store
    md_store._brief_upsert("svc", "ONE-3 first end-to-end pipeline green")
    facts, kr = _facts_kr(md_store)
    assert any("ONE-3" in f for f in facts)     # still a fact
    assert any("ONE-3" in k for k in kr)        # and a Key Result


def test_w6_superset_prunes_shorter(mem):
    from aiforge_core.memory import md_store
    md_store._brief_upsert("svc", "retries 3x")
    md_store._brief_upsert("svc", "retries 3x on NATS timeout with 5s backoff")
    facts, _ = _facts_kr(md_store)
    assert any("NATS timeout" in f for f in facts)
    assert not any(md_store._fact_body(f) == "retries 3x" for f in facts)


def test_existing_superset_skips_redundant_new(mem):
    from aiforge_core.memory import md_store
    md_store._brief_upsert("svc", "retries 3x on NATS timeout with 5s backoff")
    md_store._brief_upsert("svc", "retries 3x")   # contained → skipped
    facts, _ = _facts_kr(md_store)
    assert len([f for f in facts if "retries 3x" in f]) == 1
