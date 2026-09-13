"""Constraints as a first-class memory kind, and event-time ordering.

Two defects this covers:

1. A CONSTRAINT ("never write to X by hand", "always run tests on the nuc") was
   stored as a plain ``learning``/observation, so it rode every reaper: the
   semantic dedupe sweep collapsed restatements, the global-rescope pass demoted
   it out of global injection the moment it named a path, and at recall time it
   competed on cosine score with code chunks and could simply lose. A rule that
   silently stops being injected is worse than no rule.

2. ``recent()`` — the hot cache — ordered by ``created_at`` (WRITE time) while
   the store has carried an ``event_time`` column (when the thing actually
   happened, backdated from a ticket's created_at) that NO recall path read. So
   ingesting a three-week-old ticket today shoved it to the top of the hot cache
   ahead of yesterday's real work.
"""
from __future__ import annotations

import importlib
import time

import pytest


@pytest.fixture
def mem(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    import aiforge_core.memory.sqlite_memory as sm
    importlib.reload(sm)
    return sm


# ── 1. the kind exists and is stored ─────────────────────────────────────────

def test_constraint_kind_is_stored(mem):
    rid = mem.write_unit(text="never hand-edit ~/.aiforge/security",
                         kind="constraint", repo="demo")
    assert rid > 0
    assert mem.stats()["by_kind"].get("constraint") == 1


# ── 2. survives the reapers ──────────────────────────────────────────────────

def test_dedupe_leaves_constraints_alone(mem):
    """A restated constraint is not a duplicate to be collapsed."""
    mem.write_unit(text="always run the tests on the nuc, never the laptop",
                   kind="constraint", repo="demo")
    mem.write_unit(text="always run tests on the nuc and never on the laptop",
                   kind="constraint", repo="demo")
    out = mem.dedupe(threshold=0.5)
    assert out["removed"] == 0
    assert mem.stats()["by_kind"].get("constraint") == 2


def test_dedupe_still_collapses_observations(mem):
    """The exemption is for constraints only — the sweep still works."""
    mem.write_unit(text="the readme had three broken links in it",
                   kind="observation", repo="demo")
    mem.write_unit(text="the readme had three broken link refs in it",
                   kind="observation", repo="demo")
    out = mem.dedupe(threshold=0.5)
    assert out["removed"] >= 1


# ── 3. dedicated recall ──────────────────────────────────────────────────────

def test_constraints_recall_scopes_and_orders(mem):
    mem.write_unit(text="repo rule: build with maven not gradle",
                   kind="constraint", repo="demo")
    time.sleep(0.01)
    mem.write_unit(text="global rule: never commit secrets",
                   kind="constraint", repo=None)
    mem.write_unit(text="other repo rule: use yarn", kind="constraint",
                   repo="elsewhere")
    mem.write_unit(text="just an observation about maven", kind="observation",
                   repo="demo")

    rows = mem.constraints(repo="demo")
    texts = [r["text"] for r in rows]
    assert "repo rule: build with maven not gradle" in texts
    assert "global rule: never commit secrets" in texts
    assert "other repo rule: use yarn" not in texts       # other repo excluded
    assert "just an observation about maven" not in texts  # other kind excluded
    # newest first
    assert texts[0] == "global rule: never commit secrets"
    assert all(r["kind"] == "constraint" for r in rows)


def test_constraints_recall_empty_store(mem):
    assert mem.constraints(repo="demo") == []


# ── 4. unconditional injection ───────────────────────────────────────────────

def test_query_pins_constraints_regardless_of_relevance(mem, monkeypatch):
    """A constraint reaches the prompt even when it shares no word with the
    question and the limit is already full of better-scoring hits."""
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    mem.write_unit(text="never push directly to the production branch",
                   kind="constraint", repo="demo")
    for i in range(6):
        mem.write_unit(text=f"kafka consumer lag investigation note {i}",
                       kind="observation", repo="demo")

    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    res = uq.query("kafka consumer lag", repo="demo", limit=3)
    pinned = [h for h in res["hits"] if h.get("pinned")]
    assert [h["text"] for h in pinned] == [
        "never push directly to the production branch"]
    assert "constraint" in res["used_sources"]


def test_query_pin_can_be_disabled(mem, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    monkeypatch.setenv("AIFORGE_UMEM_CONSTRAINTS", "0")
    mem.write_unit(text="never push directly to the production branch",
                   kind="constraint", repo="demo")
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    res = uq.query("kafka consumer lag", repo="demo", limit=3)
    assert not [h for h in res["hits"] if h.get("pinned")]


# ── 5. the learner writes them ───────────────────────────────────────────────

def test_learner_persists_constraint_prefix(mem):
    from aiforge_core.runtime import learner_persist as lp
    out = lp._persist_facts_embedded(
        facts=[{"text": "CONSTRAINT: never run the suite on the laptop"},
               {"text": "DECISION: we picked NATS over Kafka"},
               {"text": "the retry service polls every 30s"}],
        repo="demo", ticket_identifier="ONE-1", session_id="s1",
        event_time=None,
    )
    assert out["written_constraints"] == 1
    assert out["written_decisions"] == 1
    assert out["written_observations"] == 1
    by_kind = mem.stats()["by_kind"]
    assert by_kind.get("constraint") == 1
    assert by_kind.get("decision") == 1
    assert by_kind.get("learning") == 1


def test_memory_write_tool_constraint_flag(mem):
    from aiforge_core.runtime.tools import memory_write as mw
    res = mw._memory_write_impl(text="always use the prod kubeconfig",
                                constraint=True, repo="demo")
    assert res["ok"] is True
    assert res["label"] == "Constraint_v1"
    assert mem.stats()["by_kind"].get("constraint") == 1


# ── 6. event time drives the hot cache ───────────────────────────────────────

def test_recent_orders_by_event_time_when_present(mem):
    """A backdated row (old event, written now) must NOT outrank a row whose
    event is newer, even though it was written later."""
    now = time.time()
    mem.write_unit(text="yesterday's real work", kind="observation",
                   repo="demo", event_time=now - 86400)
    mem.write_unit(text="a three week old ticket ingested today",
                   kind="observation", repo="demo", event_time=now - 21 * 86400)
    rows = mem.recent(limit=5, repo="demo")
    assert rows[0]["text"] == "yesterday's real work"


def test_recent_falls_back_to_created_at(mem):
    """Rows with no event_time keep write-order recency."""
    mem.write_unit(text="first written", kind="observation", repo="demo")
    time.sleep(1.01)   # created_at has second resolution in the fallback cast
    mem.write_unit(text="second written", kind="observation", repo="demo")
    rows = mem.recent(limit=5, repo="demo")
    assert rows[0]["text"] == "second written"


def test_recent_mixes_dated_and_undated(mem):
    """An undated row written now sorts above a row whose event is old."""
    mem.write_unit(text="an old event backfilled", kind="observation",
                   repo="demo", event_time=time.time() - 30 * 86400)
    mem.write_unit(text="an undated fresh write", kind="observation",
                   repo="demo")
    rows = mem.recent(limit=5, repo="demo")
    assert rows[0]["text"] == "an undated fresh write"


# ── 7. constraints reach the chat's mandatory RULES block ────────────────────

def test_rules_context_includes_stored_constraints(mem, monkeypatch, tmp_path):
    """A constraint the LEARNER captured must be injected every turn, not only
    when a recall happens to surface it."""
    from aiforge_core.runtime.chat_agent._tools import _memory as cm
    monkeypatch.setattr(cm, "_chat_repo_key", lambda _cwd: "demo")
    mem.write_unit(text="CONSTRAINT: never tag a release unless asked",
                   kind="constraint", repo="demo")
    mem.write_unit(text="always use the prod kubeconfig", kind="constraint",
                   repo=None)
    out = cm._rules_context(str(tmp_path), "unrelated question about kafka")
    assert "never tag a release unless asked" in out
    assert "always use the prod kubeconfig" in out
    assert "CONSTRAINT:" not in out          # prefix is scaffolding, not content
    assert "MANDATORY" in out


def test_rules_context_survives_a_broken_store(mem, monkeypatch, tmp_path):
    from aiforge_core.runtime.chat_agent._tools import _memory as cm

    def _boom(**_kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(mem, "constraints", _boom)
    assert cm._constraint_bullets(str(tmp_path)) == []


# ── 8. the pipeline's memory block keeps rules out of the summariser ─────────

def test_memory_block_renders_rules_verbatim(mem, monkeypatch):
    """The map→summarize fold must never see a rule: an LLM-paraphrased
    obligation is not the obligation the user set."""
    from aiforge_core.runtime import memory_block as mb

    seen: dict = {}

    class _Ticket:
        identifier = "ONE-9"
        title = "look at the kafka lag"
        body = ""
        project = "demo"

    def _fake_query(_text, **_kw):
        return {"hits": [
            {"text": "CONSTRAINT: never tag a release unless asked",
             "pinned": True, "source": "user"},
            {"text": "kafka consumer lag is a broker-side metric",
             "source": "memory", "score": 0.7},
        ], "used_sources": ["memory", "constraint"], "errors": []}

    def _fake_summarize(_text, hits):
        seen["hits"] = hits
        return "folded briefing"

    import aiforge_core.memory.recall_summary as rs
    import aiforge_core.memory.unified_query as uq
    monkeypatch.setattr(uq, "query", _fake_query)
    monkeypatch.setattr(rs, "summarize_hits", _fake_summarize)
    monkeypatch.setattr(mb, "_project_brief_prefix", lambda _t: "")

    out = mb.fetch(_Ticket())
    assert "RULES — MANDATORY" in out
    assert "never tag a release unless asked" in out
    assert "CONSTRAINT:" not in out
    # the summariser saw the ranked hit, never the rule
    assert [h["text"] for h in seen["hits"]] == [
        "kafka consumer lag is a broker-side metric"]


def test_memory_block_rules_only_still_returns_them(mem, monkeypatch):
    from aiforge_core.runtime import memory_block as mb

    class _Ticket:
        identifier = "ONE-9"
        title = "anything"
        body = ""
        project = "demo"

    import aiforge_core.memory.unified_query as uq
    monkeypatch.setattr(uq, "query", lambda _t, **_k: {
        "hits": [{"text": "always run tests on the nuc", "pinned": True}],
        "used_sources": ["constraint"], "errors": []})
    monkeypatch.setattr(mb, "_project_brief_prefix", lambda _t: "")
    out = mb.fetch(_Ticket())
    assert "always run tests on the nuc" in out


# ── 9. regressions the flow run caught ──────────────────────────────────────

def test_a_rule_that_also_matches_the_query_stays_a_rule(mem, monkeypatch):
    """Cross-dedup must drop the RANKED copy, not the pinned one.

    Dropping the pinned copy cost the rule its framing: it came back as a
    0.08-scoring search result and the pipeline's mandatory-RULES heading
    disappeared with it.
    """
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    rule = "never push straight to the production branch"
    mem.write_unit(text=rule, kind="constraint", repo="demo")
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    # a query that DOES lexically match the rule
    res = uq.query("can I push straight to the production branch", repo="demo",
                   limit=5)
    copies = [h for h in res["hits"] if (h.get("text") or "").strip() == rule]
    assert len(copies) == 1
    assert copies[0].get("pinned") is True


def test_md_ingested_rule_does_not_stutter(mem):
    """An md-captured rule stores as '<title>\\n\\n<body>' with both the same;
    rendered into a mandatory block that reads as the rule said twice."""
    mem.write_unit(text="never hand-edit the security store\n\n"
                        "never hand-edit the security store",
                   kind="constraint", repo="demo")
    rows = mem.constraints(repo="demo")
    assert rows[0]["text"] == "never hand-edit the security store"


def test_collapse_echo_leaves_real_two_part_text(mem):
    """Only an exact echo collapses — a rule with a real second paragraph keeps
    both."""
    body = "never tag a release unless asked\n\nthe release job is manual"
    mem.write_unit(text=body, kind="constraint", repo="demo")
    assert mem.constraints(repo="demo")[0]["text"] == body


# ── 10. soft-fail + md side ─────────────────────────────────────────────────

def test_query_survives_a_broken_constraint_store(mem, monkeypatch):
    """Rules must never break recall: a failing lookup is recorded, not raised."""
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    mem.write_unit(text="kafka consumer lag note", kind="observation",
                   repo="demo")

    def _boom(**_kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(mem, "constraints", _boom)
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    res = uq.query("kafka consumer lag", repo="demo", limit=3)
    assert res["hits"]                                  # recall still worked
    assert any("constraint:" in e for e in res["errors"])


def test_md_capture_keeps_the_constraint_kind(mem, tmp_path, monkeypatch):
    """The md side must file a rule under its OWN type — the global-rescope
    self-heal only ever walks type=="learning", so a rule that names a path
    stays global instead of being silently demoted."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    import aiforge_core.memory.md_store as md
    importlib.reload(md)
    res = md.capture("constraint", "never hand-edit the security store",
                     repo=None, source="user", classify=False)
    assert res.get("kind") == "constraint"
    assert any(f.get("kind") == "constraint" for f in md.list_files())


# ── 11. the defensive branches ──────────────────────────────────────────────

def test_constraints_zero_limit(mem):
    mem.write_unit(text="a rule", kind="constraint", repo="demo")
    assert mem.constraints(repo="demo", limit=0) == []


def test_constraints_soft_fails_on_a_broken_db(mem, monkeypatch):
    mem.write_unit(text="a rule", kind="constraint", repo="demo")

    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(mem._recall, "_conn", _boom)
    assert mem.constraints(repo="demo") == []


def test_constraints_dedupes_identical_text_across_scopes(mem):
    """The same rule stored repo-scoped AND global surfaces once."""
    text = "never tag a release unless asked"
    mem.write_unit(text=text, kind="constraint", repo="demo")
    mem.write_unit(text=text, kind="constraint", repo=None)
    rows = mem.constraints(repo="demo")
    assert [r["text"] for r in rows] == [text]


def test_pinned_constraints_off_when_backend_not_embedded(mem, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    mem.write_unit(text="a rule", kind="constraint", repo="demo")
    import aiforge_core.memory.backend_select as bsel
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    monkeypatch.setattr(bsel, "embedded", lambda: False)
    res = uq.query("anything", repo="demo", limit=3)
    assert not [h for h in res["hits"] if h.get("pinned")]


def test_pinned_constraints_bad_env_falls_back(mem, monkeypatch):
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    monkeypatch.setenv("AIFORGE_UMEM_CONSTRAINTS_N", "not-a-number")
    mem.write_unit(text="never tag a release unless asked", kind="constraint",
                   repo="demo")
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    res = uq.query("anything", repo="demo", limit=3)
    assert [h["text"] for h in res["hits"] if h.get("pinned")] == [
        "never tag a release unless asked"]


def test_rules_block_empty_without_pinned_hits():
    from aiforge_core.runtime import memory_block as mb
    assert mb._rules_block([{"text": "an ordinary hit"}]) == ""
    assert mb._rules_block([{"text": "   ", "pinned": True}]) == ""


def test_no_rules_means_no_constraint_source(mem, monkeypatch):
    """A store with no rules in it must not advertise the channel."""
    monkeypatch.setenv("AIFORGE_UMEM_CACHE_TTL", "0")
    mem.write_unit(text="kafka consumer lag note", kind="observation",
                   repo="demo")
    import aiforge_core.memory.unified_query as uq
    importlib.reload(uq)
    res = uq.query("kafka consumer lag", repo="demo", limit=3)
    assert "constraint" not in res["used_sources"]
    assert not [h for h in res["hits"] if h.get("pinned")]


# ── 12. the last uncovered branches of the change ───────────────────────────

def test_dedupe_scoped_to_one_repo_still_exempts_rules(mem):
    """The repo-scoped sweep takes the same exemption as the global one."""
    mem.write_unit(text="always run the tests on the nuc", kind="constraint",
                   repo="demo")
    mem.write_unit(text="always run tests on the nuc box", kind="constraint",
                   repo="demo")
    mem.write_unit(text="the readme had three broken links",
                   kind="observation", repo="demo")
    mem.write_unit(text="the readme had three broken link refs",
                   kind="observation", repo="demo")
    out = mem.dedupe(repo="demo", threshold=0.5)
    assert out["removed"] >= 1                      # observations collapsed
    assert mem.stats()["by_kind"].get("constraint") == 2


def test_render_marks_a_pinned_rule_as_a_rule():
    from aiforge_core.memory import unified_query as uq
    txt = uq.render({"used_sources": ["constraint", "memory"], "hits": [
        {"text": "never tag a release unless asked", "pinned": True},
        {"text": "the retry service polls every 30s", "source": "doer",
         "score": 0.42},
    ]})
    assert "[RULE] never tag a release unless asked" in txt
    assert "[doer|0.42]" in txt


def test_constraint_bullets_skip_a_non_embedded_backend(mem, monkeypatch,
                                                        tmp_path):
    from aiforge_core.memory import backend_select as bsel
    from aiforge_core.runtime.chat_agent._tools import _memory as cm
    mem.write_unit(text="a rule", kind="constraint", repo="demo")
    monkeypatch.setattr(bsel, "embedded", lambda: False)
    assert cm._constraint_bullets(str(tmp_path)) == []


def test_md_mirror_files_a_constraint_under_its_own_kind(mem, monkeypatch):
    """The md mirror must not file a rule as a plain learning — the
    global-rescope pass walks learnings and would demote it."""
    from aiforge_core.runtime import learner_persist as lp
    seen: list = []

    class _MD:
        @staticmethod
        def capture(kind, txt, **kw):
            seen.append((kind, txt))

    lp._mirror_one_fact({"text": "CONSTRAINT: never tag a release"}, "demo",
                        "s1", _MD)
    lp._mirror_one_fact({"text": "DECISION: NATS over Kafka"}, "demo", "s1", _MD)
    lp._mirror_one_fact({"text": "the poll interval is 30s"}, "demo", "s1", _MD)
    assert [k for k, _ in seen] == ["constraint", "project_learning", "learning"]


def test_memory_write_labels_are_unchanged_for_the_other_kinds(mem):
    from aiforge_core.runtime.tools import memory_write as mw
    dec = mw._memory_write_impl(text="DECIDED: one lock per business",
                                decision=True, repo="demo")
    obs = mw._memory_write_impl(text="the mongos pod OOMs under probe churn",
                                repo="demo")
    assert dec["label"] == "Decision_v2"
    assert obs["label"] == "Observation_v2"
    by_kind = mem.stats()["by_kind"]
    assert by_kind.get("decision") == 1
    assert by_kind.get("gotcha") == 1      # the tool's default kind
