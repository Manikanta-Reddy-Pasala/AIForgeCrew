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
    # Each rung is VISIBLY cut off (unclosed bracket) — that is what makes it a
    # fragment rather than a shorter, complete fact.
    ladder = [
        "[ c | clear l",
        "[ c | clear lockout] [ s | setup",
        "[ c | clear lockout] [ s | setup parameters] [ h | help",
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
    m.write("clear lockout", "- clear lockout takes a cphash and\n"
                             "- clear lockout takes a cphash and a setup value",
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


# ── regressions from the adversarial review ──────────────────────────────────
# Each of these was a real defect: the gate rejected true facts, or a merge
# destroyed one. Superseding DELETES with no archive, so it is the sharpest
# edge in the module.
KEEP_ANYWAY = [
    # brackets inside prose or a code span are not a truncated line
    r"The scaffold rule rejects any line matching ^\s*[|\[] at the write door.",
    "The regex is `^\\s*[|\\[]` and it rejects table rows.",
    "Build order is 1) oneshell-commons, 2) MongoDbService, 3) PosClientBackend.",
    "The CI runner exits 0 even when pytest fails :)",
    "A single backtick ` starts command substitution in bash.",
    # a lead word that merely starts with a pronoun, or names its subject next
    "There are exactly two deployment modes: Docker and Kubernetes.",
    "Same-origin policy blocks the fetch from the Electron renderer.",
    "These retries are capped at 3 attempts by the JetStream consumer.",
    "Their tokens are stored in Redis with a 7-day TTL.",
    "Is-a relationships are modelled as INFERRED edges in graphify.",
    # a fact is a fact in any script
    "Модуль синхронизации работает каждые 30 секунд.",
    "決済サービスは8090番ポートで動作する。",
]

DROP_ANYWAY = [
    "Gateway access",            # the heading, with the ### stripped
    "Final Summary",
    "Next Steps",
    "Continued in part 2",       # the compaction artifact, markup stripped
    "2026-09-16 12:00:01 INFO  [main] Started PosClientBackend in 4.213 seconds",
    "ok thanks",
    "yes exactly",
]


@pytest.mark.parametrize("text", KEEP_ANYWAY)
def test_a_real_fact_is_not_rejected_for_its_punctuation(text):
    from aiforge_core.memory.md_store import _fact
    ok, reasons = _fact.is_wellformed(text)
    assert ok, f"{text!r} rejected for {reasons}"
    # and, since repair DELETES on these, it must not be deletable either
    assert _fact.structural_issues(text) == []


@pytest.mark.parametrize("text", DROP_ANYWAY)
def test_markup_free_junk_is_still_junk(text):
    from aiforge_core.memory.md_store import _fact
    ok, _ = _fact.is_wellformed(text)
    assert not ok, f"{text!r} should have been rejected"


@pytest.mark.parametrize("old,new", [
    # a different NUMBER, not a fuller version of the same claim
    ("JetStream batch size is 50", "JetStream batch size is 500 for the DLQ job"),
    ("ADK is pinned at 2.1", "ADK is pinned at 2.11 in the nuc image"),
    ("Ollama listens on port 11434", "Ollama listens on port 114345 in the rig"),
    # a different SUBJECT that happens to share a prefix
    ("svc: rule a", "svc: rule applies only to admins"),
    ("OrderController maps /orders",
     "OrderController maps /orders-v2 to the legacy handler"),
])
def test_a_complete_fact_is_never_destroyed_by_a_longer_one(old, new):
    from aiforge_core.memory.md_store import _fact, _subject
    assert not _fact.supersedes(new, old)
    claims, action = _subject.merge_claim([old], new)
    assert action == "added"
    assert old in claims and new in claims


def test_a_visible_fragment_is_still_superseded():
    from aiforge_core.memory.md_store import _fact, _subject
    frag = "clear lockout takes a cphash and"        # dangling conjunction
    full = "clear lockout takes a cphash and a setup parameter"
    assert _fact.looks_truncated(frag)
    assert _fact.supersedes(full, frag)
    claims, action = _subject.merge_claim([frag], full)
    assert action == "superseded" and claims == [full]


@pytest.mark.parametrize("text,not_subject", [
    ("Docker builds, e.g. the nuc image, need --dns 1.1.1.1 to resolve pypi.", "e.g"),
    ("The invoice OCR confidence threshold is 0.85 for line items.", "0.85"),
    ("SELECT COUNT(*) FROM sales WHERE businessId IS NULL returns 0 rows.", "SELECT"),
    ("The API returns 404 when the JWT has expired.", "API"),
    ("`50` is the JetStream batch size.", "50"),
    ("The value `true` disables the pull loop.", "true"),
])
def test_a_generic_or_value_token_is_not_a_subject(text, not_subject):
    from aiforge_core.memory.md_store import _fact
    # a junk subject is not cosmetic: capture() FOLDS on a strong subject, so
    # every fact mentioning 0.85 or SELECT would land in one note.
    assert _fact.strong_subject(text) != not_subject


# ── repair must not touch what it does not own ───────────────────────────────
def test_repair_leaves_a_session_note_alone(cfg):
    """A session log's body is '## Run 1' sections. Judged by the fact rules it
    reads as scaffolding, and the whole file — with real facts in it — was
    archived on the first compaction."""
    from aiforge_core.memory import md_store as m
    m.write("session 7", "## Run 1\n"
                         "The change stream leader lock is renewed every 30s.\n"
                         "## Run 2\n"
                         "The pull sync default page size is 300 records.\n",
            kind="session", repo="svc")
    out = m.repair_captures()
    assert out["retired"] == 0
    assert list(m.captures_dir().glob("session-7-*.md"))


def test_repair_never_rewrites_a_body_it_cannot_reconstruct(cfg):
    """Rewriting from the claim list drops everything around the bullets — and
    an in-place rewrite is not covered by the archive."""
    from aiforge_core.memory import md_store as m
    body = ("Key findings from the outage investigation:\n"
            "\n"
            "- This repo's CI runs on Tekton, not GitHub Actions.\n"
            "- The change stream health check runs every 120 seconds.\n")
    m.write("outage findings", body, kind="learning", repo="svc")
    m.repair_captures()
    kept = next(iter(m.captures_dir().glob("outage-findings-*.md")))
    raw = kept.read_text(encoding="utf-8")
    assert "Key findings from the outage investigation:" in raw
    assert "Tekton" in raw


def test_a_collapse_archives_a_copy_first(cfg):
    from aiforge_core.memory import md_store as m
    m.write("clear lockout", "- clear lockout takes a cphash and\n"
                             "- clear lockout takes a cphash and a setup value",
            kind="learning", repo="svc")
    out = m.repair_captures()
    assert out["collapsed"] == 1
    archived = list((m.memory_dir() / "archive").rglob("pre-collapse-*.md"))
    assert archived, "a claim was dropped with no copy kept"
    assert out["dropped_claims"], "a dropped claim must be reported, not silent"


# ── the distiller must survive a box with no reasoning model ─────────────────
def test_extract_falls_back_to_the_fast_role_when_the_model_is_unreachable(
        monkeypatch):
    """The `memory` role points at a model a given box may not have loaded, so
    the call RAISES. Without a fallback the window is never distilled, and
    after enough failures the offset is force-advanced past it."""
    from aiforge_core.runtime import chat_okr_extract as ex

    calls: list[str] = []

    def _fake(role, messages, model, **kw):
        calls.append(role)
        if role == "memory":
            raise RuntimeError("LLM endpoint unreachable (127.0.0.1:1234)")
        return type("R", (), {"items": [type("I", (), {
            "text": "MessageRetryService polls every 30 seconds.",
            "kind": "learning", "subject": "MessageRetryService",
            "evidence": "application.yaml"})()]})()

    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", _fake)
    items = ex._extract("some transcript", "memory")
    assert calls == ["memory", "learner"]
    assert items and items[0].subject == "MessageRetryService"
