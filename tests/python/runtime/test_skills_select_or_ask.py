"""Tests for skills.select_or_ask — the shared ambiguity-aware scorer used
by skills, workflows, and (via an adapter) repo rules."""
from __future__ import annotations

from aiforge_core.runtime import skills as sk


def _skill(name, triggers, priority=0, always=False):
    return sk.Skill(name=name, description="", triggers=tuple(triggers),
                    body=f"body for {name}", source="", always=always,
                    priority=priority)


def test_select_or_ask_clear_winner_no_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    pool = [_skill("deploy-staging", ["deploy", "staging"]),
            _skill("unrelated", ["billing"])]
    chosen, ambiguous = sk.select_or_ask("deploy staging now", pool=pool)
    assert [s.name for s in chosen] == ["deploy-staging"]
    assert ambiguous == []


def test_select_or_ask_near_tie_is_ambiguous(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"]),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert len(ambiguous) == 1
    assert {s.name for s in ambiguous[0]} == {"deploy-staging", "deploy-prod"}
    # A best-guess is still picked (never silently drops a usable rule).
    assert len(chosen) == 1
    assert chosen[0].name in {"deploy-staging", "deploy-prod"}


def test_select_or_ask_tie_break_by_priority(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"], priority=1),
            _skill("deploy-prod", ["deploy", "prod", "release"], priority=5)]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert len(ambiguous) == 1
    assert chosen[0].name == "deploy-prod"   # higher priority wins the tie-break


def test_select_or_ask_always_on_bypasses_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"], always=True),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert "deploy-staging" in {s.name for s in chosen}   # always-on, unconditional
    assert ambiguous == []                                 # only one scored candidate


def test_select_or_ask_noise_floor_prevents_false_tie(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0.15")
    monkeypatch.setenv("AIFORGE_AMBIGUITY_FLOOR", "2.0")
    # Both candidates only weakly overlap ("the") — scores near-zero, below
    # the floor, so this must NOT be reported as ambiguous.
    pool = [_skill("alpha", ["xylophone"]), _skill("beta", ["quokka"])]
    chosen, ambiguous = sk.select_or_ask("the the the", pool=pool)
    assert ambiguous == []


def test_select_or_ask_margin_zero_disables_ambiguity(monkeypatch):
    monkeypatch.setenv("AIFORGE_AMBIGUITY_MARGIN", "0")
    pool = [_skill("deploy-staging", ["deploy", "staging", "release"]),
            _skill("deploy-prod", ["deploy", "prod", "release"])]
    chosen, ambiguous = sk.select_or_ask("deploy release now", pool=pool)
    assert ambiguous == []                          # off switch — old silent-pick behavior
    assert len(chosen) >= 1
