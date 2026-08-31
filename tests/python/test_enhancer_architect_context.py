"""Tests for the context-aware Enhancer + Architect in parallel_subtasks.

The Enhancer is mandatory in every chat mode: it fixes spelling/grammar,
recalls context (memory + recent conversation + repo README), and folds it
into a build spec. The Architect designs the file structure honoring the
repo's skills/workflows/rules. Both are hermetic here — the LLM client and the memory backend are
monkeypatched, and AIFORGE_STRUCTURED_MODE=fallback keeps structured_complete
on that patched seam, so there is no network.
"""
from __future__ import annotations

import json

import pytest

from aiforge_core.runtime import parallel_subtasks as pp


@pytest.fixture(autouse=True)
def _force_the_seam_these_tests_patch(monkeypatch):
    """Make these tests actually hermetic, which they claimed to be but were not.

    `_architect` and `_enhance` go through `structured_complete`, and in its
    default "auto" mode that uses the instructor adapter, which — by its own
    comment — "talks to the endpoint directly (bypasses client.complete)". So
    patching `llm.client.complete`, as every test here does, patched a seam the
    code never reached: the call went to the real endpoint, burned the 180s
    orchestrator timeout plus retries, and only then fell through to the patched
    function. One test took 232 SECONDS, and its result depended on that timing
    — it passed normally and FAILED under coverage, which is slower.

    `fallback` mode routes structured_complete through client.complete, the seam
    these tests patch and mean to exercise.
    """
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")


# ─── Enhancer ─────────────────────────────────────────────────────────


def test_enhance_incorporates_memory_hit(monkeypatch):
    """The enhanced spec is built from a user message that carries the
    recalled memory hit, and the enhancer's output is returned."""
    seen: dict = {}

    def fake_query(text, **kw):
        return {"hits": [{"text": "PRIOR_FACT: use SQLite for the store"}],
                "used_sources": ["memory"], "errors": []}

    def fake_complete(role, convo, **kw):
        seen["role"] = role
        seen["user"] = convo[-1]["content"]
        return "GOAL: build a store\n- uses SQLite"

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", fake_query)
    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    out = pp._enhance("buld a stor for products and customers",
                      cwd=None, repo="demo")
    assert out == "GOAL: build a store\n- uses SQLite"
    assert seen["role"] == "enhancer"
    # The recalled memory fact must be folded into the prompt sent to the LLM.
    assert "PRIOR_FACT: use SQLite for the store" in seen["user"]
    assert "RELEVANT MEMORY" in seen["user"]
    assert "buld a stor" in seen["user"]


def test_enhance_includes_recent_conversation(monkeypatch):
    seen: dict = {}

    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "spec"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    current = "add a list command to the CLI tool"
    history = [
        {"role": "user", "content": "we are building a CLI"},
        {"role": "assistant", "content": "ok, what should it do?"},
        {"role": "user", "content": current},   # current msg
    ]
    out = pp._enhance(current, history=history)
    assert out == "spec"
    assert "RECENT CONVERSATION" in seen["user"]
    assert "we are building a CLI" in seen["user"]
    # current (last) user message excluded from the conversation block, but it
    # is still the USER REQUEST.
    assert seen["user"].count(current) == 1


def test_enhance_falls_back_to_raw_on_client_error(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr("aiforge_core.llm.client.complete", boom)
    assert pp._enhance("raw prompt here that needs real enhancement") \
        == "raw prompt here that needs real enhancement"


def test_enhance_falls_back_on_empty_output(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "   ")
    assert pp._enhance("raw prompt that should enhance into something") \
        == "raw prompt that should enhance into something"


def test_enhance_respects_disable_env(monkeypatch):
    monkeypatch.setenv("AIFORGE_ENHANCER_DISABLE", "1")

    def should_not_be_called(*a, **k):
        raise AssertionError("LLM must not be called when enhancer disabled")

    monkeypatch.setattr("aiforge_core.llm.client.complete", should_not_be_called)
    assert pp._enhance("untouched prompt") == "untouched prompt"


def test_enhance_backward_compatible_single_arg(monkeypatch):
    """Existing callers pass just the prompt — must still work."""
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced")
    assert pp._enhance("a prompt to build a parser module") == "enhanced"


def test_enhance_reads_repo_readme(monkeypatch, tmp_path):
    seen: dict = {}
    (tmp_path / "README.md").write_text("# Demo Project\nThis builds widgets.\n")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def fake_complete(role, convo, **kw):
        seen["user"] = convo[-1]["content"]
        return "spec"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    pp._enhance("do a thing that builds widgets nicely", cwd=str(tmp_path))
    assert "REPO README" in seen["user"]
    assert "This builds widgets." in seen["user"]


# ─── Enhancer triviality gate (M7) ────────────────────────────────────


def test_enhance_skips_trivial_short_prompt(monkeypatch):
    # Pure-length floor (very short) + all-ack → enhancer skipped (no LLM).
    def _must_not_call(*a, **k):
        raise AssertionError("LLM/memory must not run for a trivial prompt")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _must_not_call)
    monkeypatch.setattr("aiforge_core.llm.client.complete", _must_not_call)
    assert pp._enhance("hi") == "hi"
    assert pp._enhance("thanks") == "thanks"
    assert pp._enhance("ok thanks") == "ok thanks"   # all-ack whole message


def test_enhance_skips_conversational_prompt(monkeypatch):
    def _must_not_call(*a, **k):
        raise AssertionError("LLM/memory must not run for chit-chat")

    monkeypatch.setattr("aiforge_core.memory.unified_query.query", _must_not_call)
    monkeypatch.setattr("aiforge_core.llm.client.complete", _must_not_call)
    # Whole-message conversational: a multi-word opener OR a string of acks.
    for p in ("good morning", "thank you", "ok cool", "who are you",
              "yeah cool", "ok"):
        assert pp._enhance(p) == p


def test_enhance_runs_for_short_real_imperatives(monkeypatch):
    # M7 false-negatives the old gate swallowed: short build requests and
    # ack-PREFIXED real instructions must be ENHANCED, not skipped.
    # NOTE: "fix the typo in app.py" is now handled by the Change-1 concrete
    # skip (file + action verb) and is covered in test_enhancer_concrete_skip;
    # the cases below are vague/file-less so they still enhance.
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced")
    for p in ("add a test", "add dark mode",
              "ok, refactor X", "no, use postgres instead"):
        assert pp._enhance(p) == "enhanced", p


def test_enhance_system_prompt_branches_on_informational_intent():
    """Regression for the 'tell me about this repository' bug: the enhancer's
    system prompt hardcoded a build-spec framing (goal/components/acceptance
    criteria) for EVERY request, so a pure informational question with sparse
    context got mangled into a confused non-answer. The system prompt must
    explicitly branch on intent and forbid refusing/asking the user back."""
    sys_low = pp._ENHANCE_SYS.lower()
    assert "informational" in sys_low or "question" in sys_low
    assert "never" in sys_low or "do not" in sys_low


def test_enhance_informational_question_not_forced_into_build_spec(monkeypatch):
    """'tell me about this repository' is a question, not a change request —
    the system prompt sent to the LLM must not force build-spec framing and
    must explicitly forbid a refusal/ask-back response."""
    seen: dict = {}
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})

    def fake_complete(role, convo, **kw):
        seen["system"] = convo[0]["content"]
        seen["user"] = convo[-1]["content"]
        return "What is this repository and what does it do?"

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)
    out = pp._enhance("can you tell me about this repository?", cwd=None)
    assert out == "What is this repository and what does it do?"
    sys_low = seen["system"].lower()
    assert "acceptance criteria" not in sys_low or "informational" in sys_low
    assert "never" in sys_low or "do not" in sys_low


def test_enhance_min_chars_env_override(monkeypatch):
    monkeypatch.setenv("AIFORGE_ENHANCER_MIN_CHARS", "5")
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "enhanced")
    # 10 chars, non-conversational → above the lowered threshold → enhanced.
    assert pp._enhance("parse json") == "enhanced"


# ─── Architect ────────────────────────────────────────────────────────


def test_architect_injects_skills_workflows_rules(monkeypatch):
    seen: dict = {}

    monkeypatch.setattr(
        "aiforge_core.runtime.skills.auto_context",
        lambda q, cwd=None, **k: "SKILL_BLOCK: use the doc skill")
    monkeypatch.setattr(
        "aiforge_core.runtime.workflows.auto_context",
        lambda q, cwd=None, **k: "WORKFLOW_BLOCK: ship-it procedure")
    # the architect's rule source is the SHARED bundle (_rules_context), not
    # the old repo_rules.collect — stub the seam it actually uses
    monkeypatch.setattr(
        "aiforge_core.runtime.chat_agent._rules_context",
        lambda cwd, query="": "RULE_BLOCK: always add tests")

    def fake_complete(role, convo, **kw):
        seen["role"] = role
        seen["user"] = convo[-1]["content"]
        return json.dumps({"files": [
            {"path": "db.py", "purpose": "store"},
            {"path": "main.py", "purpose": "entry"}]})

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake_complete)

    files = pp._architect("build a store", cwd="/some/repo")
    # Output contract preserved.
    assert files == [
        {"path": "db.py", "purpose": "store", "api": []},
        {"path": "main.py", "purpose": "entry", "api": []}]
    assert seen["role"] == "architect"
    # All three injected context blocks reach the architect's user message.
    assert "SKILLS:" in seen["user"]
    assert "SKILL_BLOCK: use the doc skill" in seen["user"]
    assert "WORKFLOWS:" in seen["user"]
    assert "WORKFLOW_BLOCK: ship-it procedure" in seen["user"]
    assert "REPO RULES:" in seen["user"]
    assert "RULE_BLOCK: always add tests" in seen["user"]


def test_architect_backward_compatible_no_cwd(monkeypatch):
    """Existing callers pass just the spec; with no cwd, rules are skipped
    but skills/workflows still attempt (and the JSON still parses)."""
    monkeypatch.setattr("aiforge_core.runtime.skills.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.workflows.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.repo_rules.collect",
                        lambda *a, **k: "")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: json.dumps({"files": [{"path": "x.py", "purpose": "p"}]}))
    assert pp._architect("a spec") == [
        {"path": "x.py", "purpose": "p", "api": []}]


def test_architect_context_soft_fails(monkeypatch):
    """A crash in any context source must not break the architect."""
    def boom(*a, **k):
        raise RuntimeError("skills blew up")

    monkeypatch.setattr("aiforge_core.runtime.skills.auto_context", boom)
    monkeypatch.setattr("aiforge_core.runtime.workflows.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.repo_rules.collect",
                        lambda *a, **k: "")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: json.dumps({"files": [{"path": "y.py", "purpose": "q"}]}))
    assert pp._architect("spec", cwd="/repo") == [
        {"path": "y.py", "purpose": "q", "api": []}]


# ─── Orchestrator hardening gates (single-point-of-failure trio) ────────


def test_enhancer_rejects_collapsed_spec(monkeypatch):
    """A rewrite that collapsed a long ask must NOT replace it."""
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    long_ask = ("build a spring boot service with a customer entity, a "
                "repository layer, rest endpoints for crud, validation and "
                "integration tests for every endpoint we ship to users")
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "Build a service.")
    assert pp._enhance(long_ask) == long_ask         # degenerate → raw kept


def test_enhancer_rejects_anchor_loss(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    ask = ("refactor the retry_policy handling in sync_client.py and update "
           "message_retry.py so both share one backoff helper module please")
    monkeypatch.setattr(
        "aiforge_core.llm.client.complete",
        lambda *a, **k: "Refactor the retry handling so modules share one "
                        "well-structured exponential backoff helper with tests.")
    assert pp._enhance(ask) == ask                    # every anchor dropped


def test_enhancer_keeps_good_spec(monkeypatch):
    monkeypatch.setattr("aiforge_core.memory.unified_query.query",
                        lambda *a, **k: {"hits": [], "errors": []})
    ask = "fix the retry bug in sync_client.py and add a regression test"
    good = ("GOAL: Fix the retry bug in sync_client.py.\n"
            "- Reproduce, patch the backoff loop in sync_client.py\n"
            "- Add a regression test covering the failure")
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: good)
    assert pp._enhance(ask) == good


def test_validate_plan_sanitizes_and_reports():
    files = [
        {"path": "app.py", "purpose": "x", "api": []},
        {"path": "app.py", "purpose": "dupe", "api": []},
        {"path": "../../etc/evil.py", "purpose": "escape", "api": []},
        {"path": "svc.go", "purpose": "wrong stack", "api": []},
        {"path": "Main.java", "purpose": "wrong stack", "api": []},
    ]
    clean, issues = pp._validate_plan(files)
    paths = [f["path"] for f in clean]
    assert paths == ["app.py", "svc.go", "Main.java"]   # dupe+escape dropped
    joined = " ".join(issues)
    assert "duplicate" in joined
    assert "escapes" in joined
    assert "NO test files" in joined
    assert "mixes" in joined


def test_architect_reasks_once_on_plan_defects(monkeypatch, tmp_path):
    """First plan has defects → ONE reask with the issue list → corrected
    plan wins."""
    monkeypatch.setenv("AIFORGE_STRUCTURED_MODE", "fallback")
    calls = []
    bad = ('{"files": [{"path": "a.py", "purpose": "x", "api": []},'
           ' {"path": "a.py", "purpose": "dupe", "api": []}]}')
    good = ('{"files": [{"path": "a.py", "purpose": "x", "api": []},'
            ' {"path": "tests/test_a.py", "purpose": "t", "api": []}]}')

    def fake(role, msgs, **k):
        calls.append(msgs[-1]["content"])
        return bad if len(calls) == 1 else good

    monkeypatch.setattr("aiforge_core.llm.client.complete", fake)
    monkeypatch.setattr("aiforge_core.runtime.skills.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.workflows.auto_context",
                        lambda *a, **k: "")
    monkeypatch.setattr("aiforge_core.runtime.chat_agent._rules_context",
                        lambda cwd, query="": "")
    files = pp._architect("build module a", cwd=str(tmp_path))
    assert [f["path"] for f in files] == ["a.py", "tests/test_a.py"]
    assert len(calls) == 2
    assert "PREVIOUS PLAN HAD DEFECTS" in calls[1]


def test_adk_enhancer_guard_restores_raw_ask():
    """3rd chat mode (sequential/ADK team): same degenerate gate via the
    enhancer after-callback — collapsed rewrite → raw ask restored."""
    from aiforge_core.runtime.pipeline import _make_enhancer_guard

    class _Ctx:
        def __init__(self, state):
            self.state = state

    raw = ("build a spring boot service with a customer entity, repository "
           "layer, rest endpoints for crud and integration tests for all")
    st = {"raw_ask": raw, "enhanced_body": "Build a service."}
    _make_enhancer_guard()(callback_context=_Ctx(st))
    assert st["enhanced_body"] == raw                 # degenerate → restored

    good = "GOAL: Spring Boot customer service.\n- entity, repository, rest crud endpoints\n- integration tests for all endpoints"
    st2 = {"raw_ask": raw, "enhanced_body": good}
    _make_enhancer_guard()(callback_context=_Ctx(st2))
    assert st2["enhanced_body"] == good               # good spec untouched

    st3 = {"raw_ask": raw, "enhanced_body": "ENHANCE_BLOCKED: too vague"}
    _make_enhancer_guard()(callback_context=_Ctx(st3))
    assert st3["enhanced_body"].startswith("ENHANCE_BLOCKED")  # sentinel kept
