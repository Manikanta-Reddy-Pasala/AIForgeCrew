"""Frontmatter-only cross-link unification for rules (skill/workflow/rule refs).
The rule BODY stays a terse directive; only ``links:`` + ``updated_at`` metadata
is added, and links normalize/dedupe to canonical ``kind:name`` refs.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def rules_dir(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("AIFORGE_RULES_DIR", d)
    return d


# ── the shared normalizer ────────────────────────────────────────────────────

def test_normalize_forms():
    from aiforge_core.runtime import artifact_links as al
    assert al.normalize_link("skill:jira-read") == "skill:jira-read"
    assert al.normalize_link("[[workflow/jira-ticket-to-mr]]") == \
        "workflow:jira-ticket-to-mr"
    assert al.normalize_link("rule:Jira Default") == "rule:jira-default"  # slug
    assert al.normalize_link("http://x") is None          # not an artifact ref
    assert al.normalize_link("randomtext") is None


def test_normalize_dedupes_preserves_order():
    from aiforge_core.runtime import artifact_links as al
    out = al.normalize_links(
        ["workflow:b", "skill:a", "workflow:b", "junk", "rule:c"])
    assert out == ["workflow:b", "skill:a", "rule:c"]


def test_parse_links_accepts_comma_string():
    from aiforge_core.runtime import artifact_links as al
    assert al.parse_links("skill:a, workflow:b") == ["skill:a", "workflow:b"]


# ── wired into rules (frontmatter only, body untouched) ──────────────────────

def test_rule_carries_links_and_updated_at(rules_dir):
    from aiforge_core.runtime import repo_rules as rr
    r = rr.write_rule("jira default", "Default project is ONE.",
                      triggers=["jira"],
                      links=["skill:jira-read", "[[workflow/jira-ticket-to-mr]]",
                             "garbage", "skill:jira-read"])
    assert r["ok"]
    loaded = [x for x in rr.load_global_rules() if x.name == "jira default"][0]
    assert loaded.links == ("skill:jira-read", "workflow:jira-ticket-to-mr")
    assert loaded.updated_at                      # ISO stamp present
    assert loaded.body == "Default project is ONE."   # body NEVER wrapped


def test_rule_without_links_is_clean(rules_dir):
    from aiforge_core.runtime import repo_rules as rr
    r = rr.write_rule("plain", "Do the thing.")
    loaded = [x for x in rr.load_global_rules() if x.name == "plain"][0]
    assert loaded.links == ()
    assert loaded.updated_at
    assert loaded.body == "Do the thing."


def test_legacy_rule_without_new_fields_still_parses(rules_dir):
    # a hand-made rule with no links/updated_at must load fine (back-compat)
    import os
    from aiforge_core.runtime import repo_rules as rr
    p = os.path.join(rules_dir, "old.md")
    with open(p, "w") as f:
        f.write("---\nname: old\nalwaysApply: true\n---\nLegacy rule body.")
    loaded = [x for x in rr.load_global_rules() if x.name == "old"][0]
    assert loaded.links == ()
    assert loaded.updated_at == ""
    assert loaded.body == "Legacy rule body."
