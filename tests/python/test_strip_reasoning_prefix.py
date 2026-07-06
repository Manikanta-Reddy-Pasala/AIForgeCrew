"""_strip_reasoning_prefix — a leaked chain-of-thought marker at the START of a
final answer (THOUGHT:/REASONING: …) is removed so the user sees the answer, not
the model's raw reasoning line."""
from __future__ import annotations

from aiforge_core.runtime.chat_agent import _strip_reasoning_prefix as strip


def test_strips_leading_thought_keeps_answer():
    out = strip("THOUGHT: The user asked me to plan X\n\n## Plan: real content")
    assert out.startswith("## Plan")
    assert "THOUGHT" not in out


def test_clean_answer_untouched():
    assert strip("## Plan: already clean") == "## Plan: already clean"


def test_thought_only_keeps_content_without_marker():
    # No useful tail — keep the content, just drop the marker (better than blank).
    assert strip("THOUGHT: only a thought") == "only a thought"


def test_other_markers():
    assert strip("REASONING: x\nreal answer") == "real answer"
    assert strip("ANALYSIS: y\nz") == "z"


def test_midtext_reasoning_untouched():
    # A THOUGHT that appears mid-answer is not a leaked prefix — leave it.
    t = "Here is the plan.\nTHOUGHT: this is fine mid-text"
    assert strip(t) == t


def test_empty_and_none_safe():
    assert strip("") == ""
    assert strip(None) is None
