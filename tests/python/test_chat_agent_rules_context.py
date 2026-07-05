"""_rules_context gates tagged rule bullets by topic relevance; untagged
(legacy) bullets stay always-on for backward compatibility."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import chat_agent as ca


@pytest.fixture(autouse=True)
def _clear_source_path_cache():
    # Every test here fakes `_find_by_source` under the same "demo" repo
    # name, so a hit cached by one test would leak into the next (the
    # cache is a process-global dict, keyed only by source string) —
    # clear it fresh each test, same as any other module-global test state.
    ca._source_path_cache.clear()
    yield
    ca._source_path_cache.clear()


def _fake_doc(body: str):
    return {"body": body}


def test_untagged_bullets_always_included(monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")

    class _FakePath:
        pass

    def fake_find_by_source(src):
        if src == "rules:global":
            return "PATH"
        return None

    def fake_parse(p):
        return _fake_doc("- always use yarn, not npm")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)
    out = ca._rules_context("/repo", "totally unrelated query")
    assert "always use yarn" in out


def test_tagged_bullet_gated_by_query(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")

    def fake_find_by_source(src):
        return "PATH" if src == "rules:global" else None

    def fake_parse(p):
        return _fake_doc(
            "- [triggers: deploy, staging] tag staging builds with branch\n"
            "- [triggers: billing, invoice] always round invoices to 2dp")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)

    out = ca._rules_context("/repo", "please deploy to staging now")
    assert "tag staging builds" in out
    assert "round invoices" not in out


def test_ambiguous_tagged_bullets_inject_ask_note(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")

    def fake_find_by_source(src):
        return "PATH" if src == "rules:global" else None

    def fake_parse(p):
        return _fake_doc(
            "- [triggers: deploy, staging, release] ship to staging first\n"
            "- [triggers: deploy, prod, release] ship to prod first")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)

    out = ca._rules_context("/repo", "deploy release now")
    assert "AMBIGUOUS RULE MATCH" in out
    assert "ASK" in out


def test_scorer_crash_keeps_always_on_rules(monkeypatch):
    # A defect in the shared scorer must NOT drop the legacy always-on
    # rules (the noise-fix becoming a rules-vanish bug). On scorer error,
    # always-on rules stay AND tagged bullets fail open.
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")

    def fake_find_by_source(src):
        return "PATH" if src == "rules:global" else None

    def fake_parse(p):
        return _fake_doc(
            "- always commit directly\n"
            "- [triggers: deploy] tag staging builds")

    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        fake_find_by_source)
    monkeypatch.setattr("aiforge_core.memory.md_store._parse", fake_parse)
    monkeypatch.setattr(
        "aiforge_core.runtime.skills.select_or_ask",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scorer boom")))

    out = ca._rules_context("/repo", "deploy now")
    assert "always commit directly" in out    # legacy always-on survived
    assert "tag staging builds" in out         # tagged failed open


def test_no_rules_returns_empty(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        lambda src: None)
    assert ca._rules_context("/repo", "anything") == ""
