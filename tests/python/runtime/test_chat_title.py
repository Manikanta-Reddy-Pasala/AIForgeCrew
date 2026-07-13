"""Chat title must never be the model's leaked chain-of-thought.

A local reasoning model, capped at a few tokens, emits truncated CoT first
("Thinking Process:", "The user is asking me to …"). suggest_title() must strip
that and return a clean title — falling back to the deterministic provisional
title when the model output is all reasoning.
"""
from __future__ import annotations
import pytest

from aiforge_core.runtime import chat_title as ct


@pytest.mark.parametrize("out", [
    "Thinking Process:",
    "Thinking Process:\nThe user wants a GPS report",
    "The user is asking me to check a Jira ticket CLR-2067",
    "Let me think about what a good title would be",
    "Okay, so the user needs help with the login flow",
    "<think>hmm, reasoning here</think>",
])
def test_reasoning_output_falls_back_to_clean_provisional(monkeypatch, out):
    monkeypatch.setattr(ct, "complete", lambda *a, **k: out, raising=False)
    # patch the imported symbol used inside suggest_title
    import aiforge_core.llm.client as _c
    monkeypatch.setattr(_c, "complete", lambda *a, **k: out, raising=False)
    title = ct.suggest_title("Recheck all no-GPS issues and report", role="triage")
    low = title.lower()
    assert "thinking process" not in low
    assert not low.startswith("the user")
    assert not low.startswith("let me")
    assert "<think>" not in low
    assert title.strip()                        # never empty


def test_think_tags_stripped_keeps_real_title(monkeypatch):
    import aiforge_core.llm.client as _c
    monkeypatch.setattr(_c, "complete",
                        lambda *a, **k: "<think>reason…</think>\nGPS Issue Report",
                        raising=False)
    assert ct.suggest_title("check gps", role="triage") == "GPS Issue Report"


def test_clean_title_passed_through(monkeypatch):
    import aiforge_core.llm.client as _c
    monkeypatch.setattr(_c, "complete", lambda *a, **k: "Fix Login Redirect Bug",
                        raising=False)
    assert ct.suggest_title("the login redirects wrong", role="triage") \
        == "Fix Login Redirect Bug"


def test_title_after_reasoning_is_extracted(monkeypatch):
    # reasoning model concludes with the title on the last line
    import aiforge_core.llm.client as _c
    monkeypatch.setattr(
        _c, "complete",
        lambda *a, **k: "Thinking Process:\nThe user wants X.\nGPS Outage Report",
        raising=False)
    assert ct.suggest_title("gps outage", role="triage") == "GPS Outage Report"
