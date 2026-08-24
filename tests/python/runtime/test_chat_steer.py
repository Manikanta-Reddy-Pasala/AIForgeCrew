"""chat_steer — the one source for steer/reject-guidance across all modes."""
from __future__ import annotations

from aiforge_core.runtime import chat_steer as cs


def test_user_guidance_filters_system_notes():
    assert cs.user_guidance("use single-star bold") == "use single-star bold"
    assert cs.user_guidance("  trim me  ") == "trim me"
    for sysnote in cs.SYSTEM_NOTES:
        assert cs.user_guidance(sysnote) == ""
    assert cs.user_guidance("") == ""
    assert cs.user_guidance(None) == ""


def test_reject_directive_mentions_tool_and_guidance():
    d = cs.reject_directive("jira_update", "use bullet points")
    assert "jira_update" in d
    assert "use bullet points" in d
    assert "adjust" in d.lower()
    assert "not repeat" in d.lower()


def test_steer_event_shape():
    e = cs.steer_event("also add priority")
    assert e == {"type": "thought", "role": "steer", "text": "also add priority"}


def test_applied_event_shape_and_truncates():
    e = cs.applied_event("x" * 300)
    assert e["type"] == "thought"
    assert e["role"] == "system"
    assert "Got your message" in e["text"]
    assert "x" * 120 in e["text"]
    assert len(e["text"]) < 200          # 120-char cap on the echoed text
