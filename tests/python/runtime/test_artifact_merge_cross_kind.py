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


# ── admission into the cross-kind pass ───────────────────────────────────

def test_a_cluster_decided_on_an_earlier_night_is_not_reconsidered(monkeypatch):
    """The same admission rule the per-kind passes use. Without it the sweep
    pays a model call every night to reach the verdict it already reached."""
    cluster = [_RULE, _SKILL]
    monkeypatch.setattr(am, "cross_kind_clusters", lambda *a, **k: [cluster])
    monkeypatch.setattr(am, "_seen", lambda _state, _fp: True)
    assert list(am._cross_kind_pairs(am.KINDS, {}, False)) == []


def test_force_reconsiders_it_and_files_it_under_the_most_specific_kind(monkeypatch):
    cluster = [_RULE, _SKILL]
    monkeypatch.setattr(am, "cross_kind_clusters", lambda *a, **k: [cluster])
    monkeypatch.setattr(am, "_seen", lambda _state, _fp: True)
    assert list(am._cross_kind_pairs(am.KINDS, {}, True)) == [("skills", cluster)]


def test_an_undecided_cluster_is_yielded(monkeypatch):
    cluster = [_RULE, _WORKFLOW]
    monkeypatch.setattr(am, "cross_kind_clusters", lambda *a, **k: [cluster])
    monkeypatch.setattr(am, "_seen", lambda _state, _fp: False)
    assert list(am._cross_kind_pairs(am.KINDS, {}, False)) == [("workflows", cluster)]


def test_the_pass_reads_nothing_when_it_is_switched_off(monkeypatch):
    """It is a generator so the disk is not touched until the per-kind merges
    have finished writing; switched off, it must not read at all."""
    def _explode(*_a, **_k):
        raise AssertionError("must not look for cross-kind clusters")

    monkeypatch.setenv("AIFORGE_MERGE_CROSS_KIND", "0")
    monkeypatch.setattr(am, "cross_kind_clusters", _explode)
    assert list(am._cross_kind_pairs(am.KINDS, {}, False)) == []
