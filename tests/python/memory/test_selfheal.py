"""Self-heal passes that ride along with compaction — no manual cleanup step."""
from __future__ import annotations

import pytest

from aiforge_core.memory.md_store import _selfheal


@pytest.fixture()
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("AIFORGE_OKR_SCOPE_LLM", "0")
    return tmp_path


def test_rescope_demotes_only_artifact_naming_globals(mem):
    from aiforge_core.memory.okf import store
    store.save_node("learning", "L-keep", {"scope": "global"},
                    "always run tests before committing")
    store.save_node("learning", "L-bad", {"scope": "global"},
                    "calc.py must handle division by zero")

    r = _selfheal.rescope_globals()
    assert r["demoted"] == 1

    remaining = {d["id"] for d in store.load_all("global")
                 if d.get("type") == "learning"}
    assert "L-keep" in remaining
    assert "L-bad" not in remaining        # no longer a mandatory global rule
    moved = store.read_node("learning", "L-bad")
    assert moved is not None               # ...but not deleted either
    assert moved["meta"]["demoted_from"] == "global"


def test_rescope_is_idempotent(mem):
    from aiforge_core.memory.okf import store
    store.save_node("learning", "L-bad", {"scope": "global"},
                    "demo.py needs a shebang")
    assert _selfheal.rescope_globals()["demoted"] == 1
    assert _selfheal.rescope_globals()["demoted"] == 0


def test_demoted_node_lands_in_a_real_scope_bucket(mem):
    # okf.store._scope_of maps an unrecognised scope string back to global, so
    # a plain marker like "unscoped" would leave the node exactly where it was.
    from aiforge_core.memory.okf import store
    from aiforge_core.memory.scope_guard import UNSCOPED
    store.save_node("learning", "L-x", {"scope": "global"},
                    "src/api/routes.py owns the health endpoint")
    assert _selfheal.rescope_globals()["demoted"] == 1
    assert store.read_node("learning", "L-x")["meta"]["scope"] == UNSCOPED
    assert store._scope_of("learning", {"scope": UNSCOPED}) != ""


def test_rescope_respects_its_cap(mem):
    from aiforge_core.memory.okf import store
    for i in range(5):
        store.save_node("learning", f"L-{i}", {"scope": "global"},
                        f"file{i}.py must be linted")
    assert _selfheal.rescope_globals(limit=2)["demoted"] == 2


def test_magnet_briefs_are_the_junk_named_ones(mem):
    from aiforge_core.memory import md_store as m
    m._brief_upsert("code", "a fact filed under a magnet")
    m._brief_upsert("change-stream-consumer", "a fact on a real subject")
    assert _selfheal._magnet_briefs() == ["code"]


def test_relabel_is_skipped_on_a_deterministic_pass(mem):
    # summarize=False is the offline mode; it must not wait on a model (nor on
    # a model's retry timeout), which is what made the suite hang.
    from aiforge_core.memory import md_store as m
    m._brief_upsert("code", "a fact filed under a magnet")
    out = _selfheal.run_all(summarize=False)
    assert out["relabel_magnets"]["skipped"] == "deterministic pass"
    assert "demoted" in out["rescope_globals"]


def test_relabel_moves_facts_onto_the_labelled_subject(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    m._brief_upsert("code", "the retry queue drains every 30 seconds")

    monkeypatch.setattr(
        "aiforge_core.memory.md_store._compact._topic_labels",
        lambda files, role: {f["file"]: "retry-queue" for f in files})

    r = _selfheal.relabel_magnet_facts()
    assert r["moved"] == 1
    body = m.brief_path("retry-queue").read_text()
    assert "retry queue drains" in body
    left = work_notes.parse_note(
        m.brief_path("code").read_text())["sections"].get("facts") or []
    assert left == []                      # magnet emptied, ready to sweep


def test_relabel_leaves_unplaceable_facts_alone(mem, monkeypatch):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import work_notes
    m._brief_upsert("code", "some fact the labeller cannot place")
    monkeypatch.setattr(
        "aiforge_core.memory.md_store._compact._topic_labels",
        lambda files, role: {})
    assert _selfheal.relabel_magnet_facts()["moved"] == 0
    left = work_notes.parse_note(
        m.brief_path("code").read_text())["sections"].get("facts") or []
    assert len(left) == 1                  # kept, offered again next pass


def test_passes_can_be_disabled(mem, monkeypatch):
    monkeypatch.setenv("AIFORGE_OKR_RESCOPE_GLOBALS", "0")
    monkeypatch.setenv("AIFORGE_OKR_RELABEL_MAGNETS", "0")
    assert _selfheal.rescope_globals()["skipped"] == "disabled"
    assert _selfheal.relabel_magnet_facts()["skipped"] == "disabled"


def test_compaction_reports_the_selfheal_result(mem):
    from aiforge_core.memory import md_store as m
    m.capture("topic_learning", "billing: invoices post nightly",
              repo="svc", topic="billing-pipeline")
    r = m.compact(group_by="topic", min_group=1, summarize=False)
    assert "selfheal" in r and "rescope_globals" in r["selfheal"]
