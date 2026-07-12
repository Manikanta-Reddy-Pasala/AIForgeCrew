"""Recovery for an over-aggressive reheal: re-classify each moved (source:reheal)
fact with the tightened classifier and DELETE the ones that aren't truly global
from the shared brief + their capture files. Origin repo wasn't recorded, so the
non-global ones can't be restored — they're removed, not moved back.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def test_cleanup_reheal_strips_nonglobal_keeps_global(monkeypatch, mem):
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes

    GLOBAL = "never commit directly to main"
    WRONG = "get_environ_proxies lives in src/requests/utils.py"

    # shared brief holds both facts (as reheal folded them)
    (md_store.memory_dir() / "compacted-shared.md").write_text(
        work_notes.render_note("knowledge", "shared", title="shared brief",
                               objective="Global knowledge.",
                               facts=[GLOBAL, WRONG],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    # the two source:reheal capture files
    md_store.write("g", GLOBAL, kind="learning", source="reheal", repo="shared")
    md_store.write("w", WRONG, kind="learning", source="reheal", repo="shared")

    def _fake(role, messages, model, *a, **k):
        c = messages[-1]["content"]
        scope = "global" if "commit directly" in c else "project"
        return types.SimpleNamespace(scope=scope, repo="", topic="")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.cleanup_reheal()
    assert res["checked"] == 2
    assert res["removed"] == 1

    shared = work_notes.parse_note(
        (md_store.memory_dir() / "compacted-shared.md").read_text())
    facts = shared["sections"].get("facts", [])
    assert any("commit directly" in f for f in facts)      # global kept
    assert not any("requests/utils.py" in f for f in facts)  # wrong stripped

    # the wrong capture file is gone; the global one remains
    reheal_bodies = [md_store._parse(p).get("body", "")
                     for p in md_store.memory_dir().glob("*.md")
                     if md_store._parse(p).get("source") == "reheal"]
    assert not any("requests/utils.py" in b for b in reheal_bodies)
    assert any("commit directly" in b for b in reheal_bodies)


def test_cleanup_reheal_noop_when_llm_off(mem):
    from aiforge_core.memory import md_store
    assert md_store.cleanup_reheal()["removed"] == 0
