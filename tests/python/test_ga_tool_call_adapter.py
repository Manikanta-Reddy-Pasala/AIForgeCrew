"""GA ToolCallAdapter — native vs text vs merged paths (2026-05).

These tests pin the contract for the adapter we added to
``genericagent/llmcore.py`` (commit ONE-85). The adapter is the single
normalization point for tool-call extraction across:

* Native OpenAI / Ollama Cloud / LM Studio (``message.tool_calls[]``)
* Native Anthropic (``content_blocks`` with ``type="tool_use"``)
* mlx-lm 0.31 fallback (inline ``<tool_use>{json}</tool_use>``)

GA isn't installable as a wheel and the AIForge test suite must be
runnable on a developer Mac without the NUC-deployed GA tree, so we
add the local GA path to ``sys.path`` and skip cleanly when GA isn't
available rather than fail-hard.

Coverage:

* ``from_native`` — extracts MockToolCall from content_blocks.
* ``from_text``  — extracts inline ``<tool_use>`` markers.
* ``merge`` — native wins when both present; text-fallback when native
  empty; both-empty returns empty.
* ``detect_mlx_lm_tool_call_bug`` — sniffs the mlx-lm 0.31 broken
  finish_reason=tool_calls + empty tool_calls + empty content shape.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# ─── GA import (skip cleanly when source not on disk) ─────────────────


def _maybe_ga():
    """Try importing llmcore — local checkout, $AIFORGE_GA_DIR, or NUC mount.

    Returns the module on success; ``None`` when GA not on this host
    (typical for laptop dev). The adapter tests are unit-grade so they
    don't need the live LM Studio server.
    """
    candidates = [
        os.environ.get("AIFORGE_GA_DIR"),
        "/Users/manip/genericagent",
        "/home/mani/genericagent",
    ]
    for p in candidates:
        if p and os.path.isdir(p) and os.path.isfile(os.path.join(p, "llmcore.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            try:
                import llmcore  # type: ignore
                return llmcore
            except Exception:
                continue
    return None


_llmcore = _maybe_ga()
pytestmark = pytest.mark.skipif(
    _llmcore is None,
    reason="GA llmcore.py not available on this host (set AIFORGE_GA_DIR or run on NUC)",
)


# ─── from_native ───────────────────────────────────────────────────────


class TestFromNative:
    """Native content_blocks → list[MockToolCall]."""

    def test_single_tool_use_block(self) -> None:
        blocks = [
            {"type": "text", "text": "I'll edit the file."},
            {"type": "tool_use", "id": "call_1", "name": "file_write",
             "input": {"path": "/tmp/x.py", "content": "print(1)"}},
        ]
        out = _llmcore.ToolCallAdapter.from_native(blocks)
        assert len(out) == 1
        assert out[0].function.name == "file_write"
        assert out[0].id == "call_1"
        assert json.loads(out[0].function.arguments) == {
            "path": "/tmp/x.py", "content": "print(1)",
        }

    def test_multiple_tool_use_blocks_preserved(self) -> None:
        blocks = [
            {"type": "tool_use", "id": "a", "name": "file_read", "input": {"p": "/a"}},
            {"type": "tool_use", "id": "b", "name": "file_read", "input": {"p": "/b"}},
        ]
        out = _llmcore.ToolCallAdapter.from_native(blocks)
        assert [c.function.name for c in out] == ["file_read", "file_read"]
        assert [c.id for c in out] == ["a", "b"]

    def test_empty_blocks_returns_empty(self) -> None:
        assert _llmcore.ToolCallAdapter.from_native([]) == []
        assert _llmcore.ToolCallAdapter.from_native(None) == []  # defensive

    def test_text_only_blocks_returns_empty(self) -> None:
        blocks = [{"type": "text", "text": "no tools needed"}]
        assert _llmcore.ToolCallAdapter.from_native(blocks) == []


# ─── from_text ─────────────────────────────────────────────────────────


class TestFromText:
    """Inline ``<tool_use>`` text → list[MockToolCall]."""

    def test_single_inline_marker(self) -> None:
        content = (
            "Sure, I'll update it.\n"
            '<tool_use>{"name":"file_write","arguments":{"path":"/x","content":"hi"}}</tool_use>'
        )
        calls, cleaned = _llmcore.ToolCallAdapter.from_text(content)
        assert len(calls) == 1
        assert calls[0].function.name == "file_write"
        assert "<tool_use>" not in cleaned

    def test_no_marker_returns_empty_calls(self) -> None:
        calls, cleaned = _llmcore.ToolCallAdapter.from_text("just plain text")
        assert calls == []
        assert cleaned == "just plain text"


# ─── merge ─────────────────────────────────────────────────────────────


class TestMerge:
    """Native-first merge logic (the key adapter contract)."""

    def test_native_wins_over_text(self) -> None:
        """When both native blocks AND inline markers exist, native wins."""
        content = (
            'preamble <tool_use>{"name":"text_one","arguments":{}}</tool_use> postamble'
        )
        blocks = [
            {"type": "text", "text": "preamble"},
            {"type": "tool_use", "id": "n1", "name": "native_one", "input": {"k": 1}},
        ]
        cleaned, calls = _llmcore.ToolCallAdapter.merge(content, blocks)
        assert [c.function.name for c in calls] == ["native_one"]
        # Inline markers should be stripped from cleaned content.
        assert "<tool_use>" not in cleaned

    def test_text_fallback_when_no_native(self) -> None:
        """No native tool_use blocks → fall back to text-protocol parser."""
        content = (
            'I will edit.\n<tool_use>{"name":"file_write","arguments":{"p":"/a"}}</tool_use>'
        )
        blocks = [{"type": "text", "text": content}]
        cleaned, calls = _llmcore.ToolCallAdapter.merge(content, blocks)
        assert len(calls) == 1
        assert calls[0].function.name == "file_write"

    def test_no_calls_at_all(self) -> None:
        """Plain text content + no native tool_use → no calls, content preserved."""
        cleaned, calls = _llmcore.ToolCallAdapter.merge(
            "just talking", [{"type": "text", "text": "just talking"}],
        )
        assert calls == []
        assert "just talking" in cleaned


# ─── detect_mlx_lm_tool_call_bug ───────────────────────────────────────


class TestMlxLmBugDetector:
    """The mlx-lm 0.31 native tool_calls serialization bug sniffer."""

    def test_classic_buggy_response_detected(self) -> None:
        data = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {"content": "", "tool_calls": []},
            }],
        }
        assert _llmcore.detect_mlx_lm_tool_call_bug(data) is True

    def test_normal_tool_call_not_flagged(self) -> None:
        data = {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "x", "function": {"name": "f", "arguments": "{}"}}],
                },
            }],
        }
        assert _llmcore.detect_mlx_lm_tool_call_bug(data) is False

    def test_text_content_response_not_flagged(self) -> None:
        data = {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "Hello", "tool_calls": None},
            }],
        }
        assert _llmcore.detect_mlx_lm_tool_call_bug(data) is False

    def test_empty_response_not_flagged(self) -> None:
        assert _llmcore.detect_mlx_lm_tool_call_bug({}) is False
        assert _llmcore.detect_mlx_lm_tool_call_bug({"choices": []}) is False
