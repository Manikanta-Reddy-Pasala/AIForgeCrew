"""_rules_context gates tagged rule bullets by topic relevance; untagged
(legacy) bullets stay always-on for backward compatibility."""
from __future__ import annotations

from aiforge_core.runtime import chat_agent as ca


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


def test_no_rules_returns_empty(monkeypatch):
    monkeypatch.setattr(ca, "_repo_name", lambda cwd: "demo")
    monkeypatch.setattr("aiforge_core.memory.md_store._find_by_source",
                        lambda src: None)
    assert ca._rules_context("/repo", "anything") == ""
