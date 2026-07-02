"""rule_capture threads an optional `triggers` field from classify() through
both storage paths: the md_store bullet (inline tag) and the .aiforge/rules
file (frontmatter)."""
from __future__ import annotations

from aiforge_core.runtime import rule_capture as rc


def test_parse_classification_reads_triggers():
    raw = ('{"category":"rule","scope":"project",'
          '"canonical":"tag staging builds with branch",'
          '"confidence":0.9,"task_present":false,'
          '"triggers":["deploy","staging"]}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == ["deploy", "staging"]


def test_parse_classification_defaults_triggers_empty():
    raw = ('{"category":"rule","scope":"global","canonical":"always use yarn",'
          '"confidence":0.9,"task_present":false}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == []


def test_parse_classification_sanitizes_junk_triggers():
    raw = ('{"category":"rule","scope":"global","canonical":"x",'
          '"confidence":0.9,"task_present":false,'
          '"triggers":["deploy]","...", "a,b", "  "]}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == ["deploy", "ab"]


def test_parse_classification_strips_yaml_breaking_chars():
    # Triggers with YAML-flow-breaking chars (colon-space, #, *) must be
    # sanitized so the frontmatter round-trip can't corrupt into a dict /
    # YAMLError and silently flip the rule to always-on.
    raw = ('{"category":"rule","scope":"global","canonical":"x",'
          '"confidence":0.9,"task_present":false,'
          '"triggers":["deploy: prod", "#lead", "* wild"]}')
    c = rc._parse_classification(raw)
    assert c["triggers"] == ["deploy prod", "lead", "wild"]
    for t in c["triggers"]:
        assert all(ch.isalnum() or ch in " _-" for ch in t)


def test_captured_trigger_frontmatter_round_trips(tmp_path):
    # End-to-end: a trigger that WOULD have broken YAML, once sanitized,
    # writes frontmatter that repo_rules._parse_rule_file reads back as a
    # real (non-empty) trigger list — i.e. the rule stays gated, not always-on.
    from aiforge_core.runtime import repo_rules
    raw = ('{"category":"rule","scope":"global","canonical":"tag it",'
          '"confidence":0.9,"task_present":false,'
          '"triggers":["deploy: prod"]}')
    c = rc._parse_classification(raw)
    path = rc._write_repo_rule(str(tmp_path), "r", "tag it",
                               triggers=c["triggers"])
    from pathlib import Path
    rule = repo_rules._parse_rule_file(Path(path))
    assert rule is not None
    assert rule.triggers == ("deploy prod",)   # parsed, not lost
    assert rule.always is False                 # still gated, not always-on


def test_write_repo_rule_embeds_triggers(tmp_path):
    path = rc._write_repo_rule(str(tmp_path), "deploy-staging",
                               "tag staging builds with branch",
                               triggers=["deploy", "staging"])
    text = open(path, encoding="utf-8").read()
    assert "triggers: [deploy, staging]" in text
    assert "alwaysApply: true" not in text


def test_write_repo_rule_no_triggers_stays_always(tmp_path):
    path = rc._write_repo_rule(str(tmp_path), "always-yarn", "always use yarn")
    text = open(path, encoding="utf-8").read()
    assert "alwaysApply: true" in text


def test_do_store_tags_md_bullet_with_triggers(monkeypatch, tmp_path):
    calls = {}

    def fake_append_bullet(*, source, title, bullet, kind, tags):
        calls["bullet"] = bullet

    monkeypatch.setattr("aiforge_core.memory.md_store.append_bullet",
                        fake_append_bullet)
    monkeypatch.setattr(rc, "_write_repo_rule", lambda *a, **k: None)
    c = {"category": "rule", "scope": "global",
        "canonical": "tag staging builds with branch",
        "confidence": 0.9, "task_present": False,
        "triggers": ["deploy", "staging"]}
    rc._do_store(c, rid="abc", repo="demo", session_id=None, repo_root=None)
    assert calls["bullet"] == "[triggers: deploy, staging] tag staging builds with branch"


def test_do_store_no_triggers_bullet_untagged(monkeypatch):
    calls = {}

    def fake_append_bullet(*, source, title, bullet, kind, tags):
        calls["bullet"] = bullet

    monkeypatch.setattr("aiforge_core.memory.md_store.append_bullet",
                        fake_append_bullet)
    c = {"category": "rule", "scope": "global", "canonical": "always use yarn",
        "confidence": 0.9, "task_present": False, "triggers": []}
    rc._do_store(c, rid="abc", repo="demo", session_id=None, repo_root=None)
    assert calls["bullet"] == "always use yarn"
