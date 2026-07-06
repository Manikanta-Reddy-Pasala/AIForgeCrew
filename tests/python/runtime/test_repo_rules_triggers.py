"""Trigger-scored rule matching — additive to the existing glob-only
match_rules()/collect(). A rule with globs that don't hit the ticket's
scope can still apply via a trigger-score match against the ticket text."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import repo_rules as rr


@pytest.fixture(autouse=True)
def _isolate_global_rules_dir(tmp_path, monkeypatch):
    """Isolate the operator global-rules dir to an empty tmp dir so a real
    ~/.aiforge/rules/*.md (builtin rules seeded by prior AIForgeCrew usage
    on the dev machine) doesn't leak into collect_or_ask/match_rules_with_
    triggers assertions — same guard as test_repo_rules.py. Also isolate the
    package-shipped builtin_playbooks/rules/*.md defaults for the same reason."""
    monkeypatch.setenv("AIFORGE_RULES_DIR", str(tmp_path / "_empty_rules"))
    monkeypatch.setattr(
        rr, "_builtin_rules_dir", lambda: tmp_path / "_empty_builtin")


def _rule(name, triggers=(), globs=(), always=False, body="do the thing"):
    return rr.Rule(name=name, globs=tuple(globs), always=always, body=body,
                   source=f"{name}.md", triggers=tuple(triggers))


def test_rule_triggers_field_defaults_empty():
    r = rr.Rule(name="x", globs=(), always=False, body="b", source="s")
    assert r.triggers == ()


def test_parse_rule_file_reads_triggers_frontmatter(tmp_path):
    p = tmp_path / "deploy.md"
    p.write_text(
        "---\nname: deploy-staging\ntriggers: [deploy, staging]\n---\n\n"
        "Always tag staging builds with the branch name.\n")
    r = rr._parse_rule_file(p)
    assert r is not None
    assert r.triggers == ("deploy", "staging")
    assert r.always is False


def test_match_rules_with_triggers_matches_by_query(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    rules = [_rule("deploy-staging", triggers=["deploy", "staging"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=[], query="please deploy to staging")
    assert [r.name for r in matched] == ["deploy-staging"]
    assert ambiguous == []


def test_match_rules_with_triggers_glob_still_works(monkeypatch):
    rules = [_rule("py-style", globs=["**/*.py"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=["src/a/**"], query="unrelated text")
    assert [r.name for r in matched] == ["py-style"]   # glob path unaffected


def test_match_rules_with_triggers_ambiguous_pair(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    rules = [_rule("deploy-staging", triggers=["deploy", "staging", "release"]),
             _rule("deploy-prod", triggers=["deploy", "prod", "release"])]
    matched, ambiguous = rr.match_rules_with_triggers(
        rules, scope_globs=[], query="deploy release now")
    assert len(ambiguous) == 1
    assert {r.name for r in ambiguous[0]} == {"deploy-staging", "deploy-prod"}
    assert len(matched) == 1   # best-guess still included


def test_bare_rule_no_globs_no_triggers_always_applies():
    matched, ambiguous = rr.match_rules_with_triggers(
        [_rule("bare")], scope_globs=[], query="anything")
    assert [r.name for r in matched] == ["bare"]
    assert ambiguous == []


def test_trigger_only_rule_fails_open_on_empty_query():
    # No query to score against — a trigger-only rule must still apply
    # (fail open, matching chat behavior), not silently vanish.
    matched, ambiguous = rr.match_rules_with_triggers(
        [_rule("deploy-staging", triggers=["deploy"])],
        scope_globs=[], query="")
    assert [r.name for r in matched] == ["deploy-staging"]
    assert ambiguous == []


def test_collect_or_ask_renders_and_reports_ambiguous(monkeypatch, tmp_path):
    (tmp_path / ".aiforge" / "rules").mkdir(parents=True)
    (tmp_path / ".aiforge" / "rules" / "a.md").write_text(
        "---\nname: deploy-staging\ntriggers: [deploy, staging, release]\n---\n\nStep A\n")
    (tmp_path / ".aiforge" / "rules" / "b.md").write_text(
        "---\nname: deploy-prod\ntriggers: [deploy, prod, release]\n---\n\nStep B\n")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    monkeypatch.setattr(rr, "load_global_rules", lambda: [])
    rendered, ambiguous = rr.collect_or_ask(str(tmp_path), [], "deploy release now")
    assert rendered   # best-guess rendered
    assert len(ambiguous) == 1


def test_collect_or_ask_soft_fails_to_empty(monkeypatch):
    rendered, ambiguous = rr.collect_or_ask("/no/such/dir", [], "anything")
    assert rendered == ""
    assert ambiguous == []
