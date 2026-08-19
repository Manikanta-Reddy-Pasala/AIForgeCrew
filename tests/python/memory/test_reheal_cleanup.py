"""Recovery for an over-aggressive reheal: re-classify each moved (source:reheal)
fact with the tightened classifier and DELETE the ones that aren't truly global
from the shared brief + their capture files. Origin repo wasn't recorded, so the
non-global ones can't be restored — they're removed, not moved back.
"""
from __future__ import annotations

import re
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
    (md_store.brief_path("shared")).write_text(
        work_notes.render_note("knowledge", "shared", title="shared brief",
                               objective="Global knowledge.",
                               facts=[GLOBAL, WRONG],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    # the two source:reheal capture files
    md_store.write("g", GLOBAL, kind="learning", source="reheal", repo="shared")
    md_store.write("w", WRONG, kind="learning", source="reheal", repo="shared")

    def _fake(role, messages, model, *a, **k):
        # batched classifier: one entry per "[n] item" line
        lines = [ln for ln in messages[-1]["content"].splitlines()
                 if re.match(r"^\[\d+\] ", ln)]
        return types.SimpleNamespace(items=[
            types.SimpleNamespace(index=int(re.match(r"^\[(\d+)\] ", ln).group(1)),
                                  scope=("global" if "commit directly" in ln else "project"),
                                  repo="", topic="")
            for ln in lines])

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    res = md_store.cleanup_reheal()
    assert res["checked"] == 2
    assert res["removed"] == 1

    shared = work_notes.parse_note(
        (md_store.brief_path("shared")).read_text())
    facts = shared["sections"].get("facts", [])
    assert any("commit directly" in f for f in facts)      # global kept
    assert not any("requests/utils.py" in f for f in facts)  # wrong stripped

    # the wrong capture file is gone; the global one remains
    reheal_bodies = [md_store._parse(p).get("body", "")
                     for p in md_store.captures_dir().glob("*.md")
                     if md_store._parse(p).get("source") == "reheal"]
    assert not any("requests/utils.py" in b for b in reheal_bodies)
    assert any("commit directly" in b for b in reheal_bodies)


def test_cleanup_reheal_noop_when_llm_off(mem):
    from aiforge_core.memory import md_store
    assert md_store.cleanup_reheal()["removed"] == 0


def test_cleanup_reheal_does_not_delete_when_the_model_never_answered(monkeypatch,
                                                                      mem):
    """A failed scope batch used to read as N confident "project" verdicts —
    and this function DELETES what isn't global. One 502 wiped the batch."""
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "1")
    from aiforge_core.memory import md_store
    from aiforge_core.runtime import work_notes

    GLOBAL = "never commit directly to main"
    WRONG = "get_environ_proxies lives in src/requests/utils.py"
    (md_store.brief_path("shared")).write_text(
        work_notes.render_note("knowledge", "shared", title="shared brief",
                               objective="Global knowledge.",
                               facts=[GLOBAL, WRONG],
                               updated_at="2026-07-12T00:00:00+00:00"),
        encoding="utf-8")
    md_store.write("g", GLOBAL, kind="learning", source="reheal", repo="shared")
    md_store.write("w", WRONG, kind="learning", source="reheal", repo="shared")

    def _down(*a, **k):
        raise RuntimeError("502 from the endpoint")

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _down)
    res = md_store.cleanup_reheal()
    assert res["removed"] == 0                    # nothing destroyed on no evidence
    facts = work_notes.parse_note(
        (md_store.brief_path("shared")).read_text())["sections"].get("facts", [])
    assert len(facts) == 2
