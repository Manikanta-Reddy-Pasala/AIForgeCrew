"""OKR is USED properly downstream: an auto-injected memory brief feeds the
model only KNOWLEDGE (Facts + consolidated body), never the envelope's own
Objective boilerplate / title / sentinel. Recall-ingest strips identically.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture()
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


def test_knowledge_text_strips_envelope():
    from aiforge_core.runtime import work_notes
    note = work_notes.render_note(
        "knowledge", "svc", title="svc memory",
        objective="Keep durable knowledge current — scaffolding boilerplate.",
        facts=["db via gateway only", "use flyway"],
        body_md="Consolidated prose about the service.")
    k = work_notes.knowledge_text(note)
    assert "db via gateway only" in k and "use flyway" in k
    assert "Consolidated prose about the service." in k
    assert "boilerplate" not in k          # Objective dropped
    assert "aiforge:body" not in k         # sentinel dropped
    assert "# svc memory" not in k         # title dropped


def test_knowledge_text_legacy_fallback():
    from aiforge_core.runtime import work_notes
    assert work_notes.knowledge_text("just plain prose") == "just plain prose"


def test_project_brief_injects_clean_knowledge(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.runtime import context_bundle as cb
    m._brief_upsert("svc", "db via gateway only", topic="arch")
    m._brief_upsert("svc", "use flyway not ddl-auto")
    d = m.read_file("compacted-svc")
    k = cb._brief_knowledge(d)
    assert "db via gateway only" in k and "flyway" in k
    assert "Keep durable" not in k         # Objective boilerplate not injected
    assert "## Objective" not in k
    assert "aiforge:body" not in k


def test_brief_knowledge_empty_and_none(cfg):
    from aiforge_core.runtime import context_bundle as cb
    assert cb._brief_knowledge(None) == ""
    assert cb._brief_knowledge({"body": ""}) == ""
