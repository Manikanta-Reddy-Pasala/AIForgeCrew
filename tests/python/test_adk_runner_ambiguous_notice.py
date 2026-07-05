"""Autonomous tickets never block on ambiguous rule matches — best-guess is
already applied by collect_or_ask; this only checks the non-blocking notice
event fires (and does NOT fire for interactive tickets, which clarify.py
already handled before this code runs)."""
from __future__ import annotations

from types import SimpleNamespace

import aiforge_core.runtime.adk_runner as ar


def _ticket(interactive=False):
    return SimpleNamespace(id=7, identifier="T-7", title="Deploy",
                           body="deploy release now", project=None,
                           metadata={"interactive": interactive})


def test_autonomous_ambiguous_rule_emits_notice(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    monkeypatch.setattr(
        "aiforge_core.runtime.repo_rules.collect_or_ask",
        lambda *a, **k: ("rendered rules", [[r1, r2]]))
    events = []
    monkeypatch.setattr(
        "aiforge_core.tickets.store.add_event",
        lambda *a, **k: events.append((a, k)))
    ar._emit_ambiguous_rule_notice(_ticket(interactive=False),
                                   [[r1, r2]])
    assert len(events) == 1
    assert events[0][0][2] == "ambiguous_rule_match"


def test_interactive_ticket_no_notice(monkeypatch):
    from aiforge_core.runtime.repo_rules import Rule
    r1 = Rule(name="deploy-staging", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    r2 = Rule(name="deploy-prod", globs=(), always=False, body="b",
              source="s", triggers=("deploy",))
    events = []
    monkeypatch.setattr(
        "aiforge_core.tickets.store.add_event",
        lambda *a, **k: events.append((a, k)))
    ar._emit_ambiguous_rule_notice(_ticket(interactive=True), [[r1, r2]])
    assert events == []   # clarify.py already handled it — no double notice


# ── enhancer-blocked sentinel — tickets are unattended, so this stands in
# for the clarifying question a chat agent could ask a human ─────────────

def test_enhancer_block_reason_none_when_normal_body():
    assert ar._enhancer_block_reason(
        {"enhanced_body": "# Add a delete method\n\n## Goal\n..."}) is None


def test_enhancer_block_reason_none_when_missing_key():
    assert ar._enhancer_block_reason({}) is None
    assert ar._enhancer_block_reason(None) is None


def test_enhancer_block_reason_extracts_reason():
    r = ar._enhancer_block_reason(
        {"enhanced_body": "ENHANCE_BLOCKED: no goal extractable from body"})
    assert r == "no goal extractable from body"


def test_enhancer_block_reason_falls_back_when_reason_empty():
    r = ar._enhancer_block_reason({"enhanced_body": "ENHANCE_BLOCKED:"})
    assert r == "ticket body too vague for the enhancer to act on"
