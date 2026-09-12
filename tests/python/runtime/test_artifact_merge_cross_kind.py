"""One instruction saved as a rule AND as a skill never met itself.

``find_clusters(kind)`` only ever compared a kind with itself, so "run the
tests before pushing" written once as a rule and once as a skill stayed as two
artifacts forever — both injected, both prompt overhead. The sweep now also
pools the three kinds together, and a mixed cluster collapses into the MOST
SPECIFIC kind present: a workflow spells out steps, a skill explains an
approach, a rule only asserts, and folding the specific into the general is the
lossy direction.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime import artifact_merge as am


def _item(kind: str, name: str, body: str, desc: str = "", triggers=()):
    return am.item_from(kind, name, desc, triggers, body, source=f"/x/{name}.md")


# The same instruction, three resolutions.
_BODY = ("Run the full test suite before pushing. If anything fails, fix it "
         "first; never push a red suite to main.")
_RULE = _item("rules", "test before push", _BODY, "always test before pushing")
_SKILL = _item("skills", "testing before a push", _BODY,
               "run the tests before pushing")
_WORKFLOW = _item("workflows", "pre-push checks", _BODY + " Then push.",
                  "the pre-push procedure")
_OTHER = _item("rules", "python imports", "Group imports stdlib, third-party, "
               "local, each block alphabetised by module name.",
               "import ordering")


@pytest.fixture
def _library(monkeypatch):
    """A library holding the same instruction under two kinds, plus a
    genuinely different rule that must never be dragged in."""
    pool = {"rules": [_RULE, _OTHER], "skills": [_SKILL], "workflows": []}
    monkeypatch.setattr(am, "load", lambda kind: pool.get(kind, []))
    monkeypatch.setattr(am, "mergeable", lambda _i: True)
    return pool


def test_a_rule_and_a_skill_saying_the_same_thing_are_one_cluster(_library):
    clusters = am.cross_kind_clusters()
    assert len(clusters) == 1
    assert {i.kind for i in clusters[0]} == {"rules", "skills"}
    assert _OTHER.name not in {i.name for i in clusters[0]}


def test_a_duplicate_inside_one_kind_is_left_to_the_per_kind_pass(monkeypatch):
    """Only MIXED clusters come back here — merging a same-kind pair twice
    would be wasted model calls."""
    twin = _item("rules", "always run tests", _BODY, "test before pushing")
    monkeypatch.setattr(am, "load",
                        lambda kind: [_RULE, twin] if kind == "rules" else [])
    monkeypatch.setattr(am, "mergeable", lambda _i: True)
    assert am.cross_kind_clusters() == []


@pytest.mark.parametrize("cluster,expected", [
    ([_RULE, _SKILL], "skills"),
    ([_RULE, _WORKFLOW], "workflows"),
    ([_SKILL, _WORKFLOW], "workflows"),
    ([_RULE, _SKILL, _WORKFLOW], "workflows"),
    ([_RULE], "rules"),
])
def test_the_most_specific_kind_wins(cluster, expected):
    assert am.target_kind(cluster) == expected


def test_the_merge_prompt_tells_the_model_the_cluster_is_mixed():
    """It has to SEE the mix: the workflow's steps and the rule's one-liner are
    the same instruction at different resolutions, and the steps must survive."""
    prompt = am._merge_prompt("workflows", [_RULE, _WORKFLOW])
    assert "mix of" in prompt
    assert "rules" in prompt
    assert "workflows" in prompt
    assert "keep every concrete step" in prompt.lower()


def test_a_single_kind_prompt_is_unchanged():
    prompt = am._merge_prompt("rules", [_RULE, _OTHER])
    assert "mix of" not in prompt
    assert "These 2 rules" in prompt


def test_cross_kind_merging_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_MERGE_CROSS_KIND", "0")
    assert am._cross_kind_enabled() is False
    monkeypatch.delenv("AIFORGE_MERGE_CROSS_KIND")
    assert am._cross_kind_enabled() is True


def test_a_candidate_that_is_not_on_disk_can_still_be_scored():
    """item_from exists so the WRITER can ask 'do we already have this?' with
    the same similarity the sweep uses — two definitions would drift."""
    cand = am.item_from("skills", "testing before a push", "run tests", [], _BODY)
    assert am.similarity(cand, _SKILL) > 0.9
    assert am.similarity(cand, _OTHER) < 0.5
