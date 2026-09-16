"""A memory is a FACT: a standalone claim about a named subject.

The strings in these tests are real ones pulled out of a live store — a CLI
usage fragment, a table row, a compaction artifact, a raw chat turn, and the
same claim saved three times at three truncation lengths. Every one of them was
a permanent memory before the fact gate existed.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", tempfile.mkdtemp() + "/m.db")
    return None


# ── the gate ─────────────────────────────────────────────────────────────────
JUNK = [
    "Final",
    "[ -c | --clear-l",
    "[ c | clear lockout] [ s | setup parameters] [",
    "| GET /api/version → one JSON payload |",
    "| app release baseos ...",
    "**Continued in:** [part 2](compacted notes 2.md)",
    "can you do chnages",
    "can you add gitlab ci file for this repo",
    "attahced his solution",
    "this is the data architect problem",
    "### Gateway access",
]

FACTS = [
    "LM Studio rejects an OBJECT tool_choice and requires the string form.",
    "Gateway VMs are the '.1' host in their subnet.",
    "MessageRetryService polls pendingToSync every 30 seconds in batches of 50.",
    "The TPM DA parameters on H2 were retuned to MAX_AUTH_FAIL 32.",
]


@pytest.mark.parametrize("text", JUNK)
def test_junk_is_not_a_fact(text):
    from aiforge_core.memory.md_store import _fact
    ok, reasons = _fact.is_wellformed(text)
    assert not ok, f"{text!r} should have been rejected"
    assert reasons


@pytest.mark.parametrize("text", FACTS)
def test_real_facts_survive_the_gate(text):
    from aiforge_core.memory.md_store import _fact
    ok, reasons = _fact.is_wellformed(text)
    assert ok, f"{text!r} rejected for {reasons}"


def test_gate_can_be_switched_off(monkeypatch):
    from aiforge_core.memory.md_store import _fact
    monkeypatch.setenv("AIFORGE_MEMORY_FACT_GATE", "0")
    ok, _ = _fact.is_wellformed("Final")
    assert ok                      # legacy behaviour: any non-empty string


# ── subject ──────────────────────────────────────────────────────────────────
def test_subject_prefers_a_quoted_identifier():
    from aiforge_core.memory.md_store import _fact
    assert _fact.derive_subject(
        "the `MessageRetryService` polls every 30s") == "MessageRetryService"


def test_subject_finds_a_path_or_camelcase():
    from aiforge_core.memory.md_store import _fact
    assert _fact.derive_subject(
        "run.sh/_ca_bootstrap returns early when no CA is readable"
    ) == "run.sh/_ca_bootstrap"
    assert _fact.derive_subject(
        "The MongoDbService gateway is mandatory") == "MongoDbService"


def test_title_is_the_subject_not_the_first_70_chars():
    from aiforge_core.memory.md_store import _fact
    claim = ("The MongoDbService gateway is mandatory and no service may query "
             "MongoDB directly under any circumstance whatsoever")
    assert _fact.title_for("", claim) == "MongoDbService"


# ── the truncation ladder ────────────────────────────────────────────────────
def test_a_fuller_claim_supersedes_the_fragment_it_grew_from():
    from aiforge_core.memory.md_store import _fact, _subject
    ladder = [
        "clear lockout takes a cphash",
        "clear lockout takes a cphash value",
        "clear lockout takes a cphash value from setup parameters",
    ]
    claims: list[str] = []
    for rung in ladder:
        claims, _ = _subject.merge_claim(claims, rung)
    assert claims == [ladder[-1]]
    assert _fact.supersedes(ladder[-1], ladder[0])
    assert not _fact.supersedes(ladder[0], ladder[-1])


def test_a_rephrasing_is_a_duplicate_not_a_new_claim():
    from aiforge_core.memory.md_store import _subject
    claims, action = _subject.merge_claim(["Gateway VMs are the .1 host"],
                                          "gateway vms are the .1 host!")
    assert action == "duplicate"
    assert len(claims) == 1


# ── capture ──────────────────────────────────────────────────────────────────
def test_capture_drops_a_non_fact(cfg):
    from aiforge_core.memory import md_store as m
    res = m.capture("learning", "attahced his solution", repo="svc",
                    classify=False)
    assert res.get("skipped") == "not_a_fact"
    assert not list(m.captures_dir().glob("*.md"))


def test_capture_folds_a_second_claim_into_the_subject_note(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _subject
    first = m.capture("project_learning",
                      "MessageRetryService polls pendingToSync every 30 seconds.",
                      repo="svc", classify=False, subject="MessageRetryService")
    second = m.capture("project_learning",
                       "MessageRetryService NAKs a failed message with a 5-10s delay.",
                       repo="svc", classify=False, subject="MessageRetryService")
    assert first["action"] == "created"
    assert second["action"] == "added"
    files = list(m.captures_dir().glob("messageretryservice-*.md"))
    assert len(files) == 1, [f.name for f in files]
    body = files[0].read_text(encoding="utf-8")
    assert "subject: MessageRetryService" in body
    assert len(_subject.claims_of(body.split("---", 2)[-1])) == 2


def test_capture_stamps_evidence(cfg):
    from aiforge_core.memory import md_store as m
    m.capture("learning", "Percona mongos caps logical sessions per node.",
              repo="svc", classify=False, evidence="db.adminCommand killAllSessions")
    body = next(iter(m.captures_dir().glob("*.md"))).read_text(encoding="utf-8")
    assert "evidence: db.adminCommand killAllSessions" in body


# ── self-repair ──────────────────────────────────────────────────────────────
def test_repair_retires_non_fact_notes_and_collapses_ladders(cfg):
    from aiforge_core.memory import md_store as m
    from aiforge_core.memory.md_store import _subject
    # written the OLD way — straight through write(), no gate
    m.write("Final", "Final", kind="topic_learning", repo="svc")
    m.write("clear lockout", "- clear lockout takes a cphash\n"
                             "- clear lockout takes a cphash value from setup",
            kind="learning", repo="svc")
    m.write("MongoDbService", "MongoDbService is the mandatory MongoDB gateway.",
            kind="project_learning", repo="svc")

    out = m.repair_captures()
    assert out["ok"] and out["retired"] == 1 and out["collapsed"] == 1
    names = sorted(p.name.split("-")[0] for p in m.captures_dir().glob("*.md"))
    assert "final" not in names
    ladder = next(iter(m.captures_dir().glob("clear-lockout-*.md")))
    assert len(_subject.claims_of(
        ladder.read_text(encoding="utf-8").split("---", 2)[-1])) == 1


def test_repair_dry_run_changes_nothing(cfg):
    from aiforge_core.memory import md_store as m
    m.write("Final", "Final", kind="topic_learning", repo="svc")
    out = m.repair_captures(dry_run=True)
    assert out["retired"] == 1
    assert list(m.captures_dir().glob("final-*.md"))


# ── the model role ───────────────────────────────────────────────────────────
def test_memory_work_runs_on_a_thinking_role(monkeypatch):
    from aiforge_core.config import model_registry
    from aiforge_core.memory.md_store import _role
    monkeypatch.delenv("AIFORGE_MEMORY_MODEL_ROLE", raising=False)
    assert _role.memory_role() == "memory"
    assert _role.is_thinking_role("memory")
    assert not model_registry.is_fast_role("memory")
    # ctx_memory stays FAST — it only assembles context, it doesn't judge.
    assert model_registry.is_fast_role("ctx_memory")


def test_memory_role_is_overridable(monkeypatch):
    from aiforge_core.memory.md_store import _role
    monkeypatch.setenv("AIFORGE_MEMORY_MODEL_ROLE", "learner")
    assert _role.memory_role() == "learner"
    assert not _role.is_thinking_role("memory")
