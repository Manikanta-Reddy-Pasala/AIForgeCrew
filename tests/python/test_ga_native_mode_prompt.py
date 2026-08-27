"""GA ToolClient — native-mode prompt suppression (2026-05, ticket ONE-87).

Pins the contract that when the underlying GA session has native
``tool_choice`` (``required`` / ``auto``) plus a ``tools`` schema set,
``ToolClient.chat`` builds a prompt that does NOT contain the GA
text-protocol scaffolding (``### Interaction Protocol``, ``Format:
<tool_use>{...}</tool_use>``, ``### Tools (mounted, ...)``).

Reason: qwen3-coder:480b on Ollama Cloud was given mixed signals
(native ``tools`` payload AND text-protocol prompt) and emitted only
``<thinking>``/``<summary>`` text with zero ``tool_calls``.  Stripping
the duplicate text-protocol from the prompt forces the model to use
the native channel.

Regression gate: when ``tool_choice`` is None (the legacy mlx-lm 0.31
+ LM Studio path), the text-protocol scaffolding MUST still appear —
those backends silently drop native ``tool_calls[]`` so the prompt is
the only signal that works.

GA isn't pip-installed; locate ``llmcore.py`` on disk and skip cleanly
when the source tree isn't present (laptop dev without NUC mount).
"""
from __future__ import annotations

import os
import sys

import pytest


def _maybe_ga():
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

# The marker is what SELECTS these (they are deselected by default in
# pyproject). The skipif is the module's own docstring contract: when the
# tests are asked for on a box with no GA checkout, say so — don't die with
# ``'NoneType' object has no attribute 'ToolClient'`` ten times over.
pytestmark = [
    pytest.mark.live_ga,
    pytest.mark.skipif(
        _llmcore is None,
        reason="GA checkout not found — set AIFORGE_GA_DIR to a tree with llmcore.py",
    ),
]


# ─── Hermetic fake backend ─────────────────────────────────────────────


class _FakeBackend:
    """Minimal stand-in for ``BaseSession`` that ToolClient reads from.

    ToolClient.chat only touches: ``backend.tool_choice``,
    ``backend.tools``, ``backend.name``, ``backend.ask()``. We never
    actually invoke ``ask`` here — the test calls
    ``_build_protocol_prompt`` directly so no network / model is in
    play.
    """

    def __init__(self, *, tool_choice=None, tools=None):
        self.tool_choice = tool_choice
        self.tools = tools
        self.name = "fake-backend"

    def ask(self, prompt, stream=False):  # pragma: no cover — not exercised
        raise NotImplementedError


# ─── Reusable fixtures ─────────────────────────────────────────────────


SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write text content to a file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

SAMPLE_MESSAGES = [
    {"role": "system", "content": "You are a coding agent."},
    {"role": "user", "content": "Create /tmp/x.py with `print(1)`."},
]


# Sentinels we look for in the assembled prompt. These are the exact
# substrings _prepare_tool_instruction emits in the legacy path.
_PROTOCOL_HEADER_EN = "### Interaction Protocol"
_PROTOCOL_HEADER_ZH = "### 交互协议"
_FORMAT_LINE = 'Format: ```<tool_use>'
_TOOLS_HEADER_EN = "### Tools (mounted, always in effect):"
_TOOLS_HEADER_ZH = "### 工具库状态"  # appears in both fresh + cached zh paths
_NATIVE_NUDGE_EN = "Use the provided tools to complete the task. Emit native tool calls."
_NATIVE_NUDGE_ZH = "请使用提供的工具完成任务"


def _make_client(tool_choice, tools):
    backend = _FakeBackend(tool_choice=tool_choice, tools=tools)
    return _llmcore.ToolClient(backend), backend


# ─── Native-mode suppresses text-protocol ─────────────────────────────


class TestNativeModeSuppressesTextProtocol:
    def test_required_tool_choice_skips_protocol(self, monkeypatch) -> None:
        monkeypatch.setenv("GA_LANG", "en")
        client, _ = _make_client(tool_choice="required", tools=SAMPLE_TOOLS)
        prompt = client._build_protocol_prompt(
            SAMPLE_MESSAGES, SAMPLE_TOOLS, native_mode=True,
        )
        assert _PROTOCOL_HEADER_EN not in prompt
        assert _FORMAT_LINE not in prompt
        assert _TOOLS_HEADER_EN not in prompt
        # Tool schema JSON must NOT be embedded — backend payload owns it.
        assert '"file_write"' not in prompt
        # Native nudge present.
        assert _NATIVE_NUDGE_EN in prompt

    def test_auto_tool_choice_also_native(self, monkeypatch) -> None:
        monkeypatch.setenv("GA_LANG", "en")
        client, _ = _make_client(tool_choice="auto", tools=SAMPLE_TOOLS)
        prompt = client._build_protocol_prompt(
            SAMPLE_MESSAGES, SAMPLE_TOOLS, native_mode=True,
        )
        assert _PROTOCOL_HEADER_EN not in prompt
        assert _FORMAT_LINE not in prompt
        assert _NATIVE_NUDGE_EN in prompt

    def test_dict_tool_choice_treated_as_native(self, monkeypatch) -> None:
        monkeypatch.setenv("GA_LANG", "en")
        client, _ = _make_client(
            tool_choice={"type": "function", "function": {"name": "file_write"}},
            tools=SAMPLE_TOOLS,
        )
        prompt = client._build_protocol_prompt(
            SAMPLE_MESSAGES, SAMPLE_TOOLS, native_mode=True,
        )
        assert _FORMAT_LINE not in prompt
        assert _NATIVE_NUDGE_EN in prompt

    def test_zh_native_mode_uses_chinese_nudge(self, monkeypatch) -> None:
        monkeypatch.delenv("GA_LANG", raising=False)
        client, _ = _make_client(tool_choice="required", tools=SAMPLE_TOOLS)
        prompt = client._build_protocol_prompt(
            SAMPLE_MESSAGES, SAMPLE_TOOLS, native_mode=True,
        )
        assert _PROTOCOL_HEADER_ZH not in prompt
        assert _FORMAT_LINE not in prompt
        assert _NATIVE_NUDGE_ZH in prompt


# ─── Legacy text-protocol path preserved ──────────────────────────────


class TestTextProtocolPathPreserved:
    """Regression gate: tool_choice=None must NOT trigger native_mode.

    mlx-lm 0.31 + LM Studio drops native ``tool_calls[]`` so the
    text-protocol scaffolding is the only signal that produces tool
    calls. Default behaviour (no flag) must be byte-for-byte the same
    as before this patch landed.
    """

    def test_no_tool_choice_keeps_protocol(self, monkeypatch) -> None:
        monkeypatch.setenv("GA_LANG", "en")
        client, _ = _make_client(tool_choice=None, tools=None)
        prompt = client._build_protocol_prompt(SAMPLE_MESSAGES, SAMPLE_TOOLS)
        assert _PROTOCOL_HEADER_EN in prompt
        assert _FORMAT_LINE in prompt
        assert _TOOLS_HEADER_EN in prompt
        # Tool schema embedded in prompt (text-protocol contract).
        assert '"file_write"' in prompt

    def test_explicit_native_mode_false_keeps_protocol(self, monkeypatch) -> None:
        monkeypatch.setenv("GA_LANG", "en")
        client, _ = _make_client(tool_choice=None, tools=None)
        prompt = client._build_protocol_prompt(
            SAMPLE_MESSAGES, SAMPLE_TOOLS, native_mode=False,
        )
        assert _PROTOCOL_HEADER_EN in prompt
        assert _FORMAT_LINE in prompt


# ─── ToolClient.chat infers native_mode from backend ──────────────────


class TestChatInfersNativeMode:
    """End-to-end: ``ToolClient.chat`` must pick up ``backend.tool_choice``.

    We don't actually drive ``backend.ask`` (that would need a real
    LLM); we monkeypatch ``_build_protocol_prompt`` to capture the
    ``native_mode`` flag and assert it's True/False as expected.
    """

    def _capture_native_mode(self, monkeypatch, *, tool_choice, tools):
        captured = {}
        client, backend = _make_client(tool_choice=tool_choice, tools=tools)

        def _fake_build(messages, t, native_mode=False):
            captured["native_mode"] = native_mode
            return "STUB_PROMPT"

        def _fake_ask(prompt, stream=False):
            # Empty generator; chat() iterates but extracts nothing.
            if False:
                yield ""
            return

        monkeypatch.setattr(client, "_build_protocol_prompt", _fake_build)
        monkeypatch.setattr(backend, "ask", _fake_ask)
        # Drain the generator returned by ``chat``.
        gen = client.chat(SAMPLE_MESSAGES, SAMPLE_TOOLS)
        try:
            for _ in gen:
                pass
        except StopIteration:
            pass
        return captured

    def test_required_with_tools_flips_native_on(self, monkeypatch) -> None:
        captured = self._capture_native_mode(
            monkeypatch, tool_choice="required", tools=SAMPLE_TOOLS,
        )
        assert captured.get("native_mode") is True

    def test_auto_with_tools_flips_native_on(self, monkeypatch) -> None:
        captured = self._capture_native_mode(
            monkeypatch, tool_choice="auto", tools=SAMPLE_TOOLS,
        )
        assert captured.get("native_mode") is True

    def test_no_tool_choice_keeps_native_off(self, monkeypatch) -> None:
        captured = self._capture_native_mode(
            monkeypatch, tool_choice=None, tools=None,
        )
        assert captured.get("native_mode") is False

    def test_tool_choice_required_but_no_tools_keeps_native_off(self, monkeypatch) -> None:
        """Defensive: if a session has tool_choice but somehow no tools, don't
        suppress the text-protocol prompt — that would leave the model with
        nothing to call.
        """
        captured = self._capture_native_mode(
            monkeypatch, tool_choice="required", tools=None,
        )
        assert captured.get("native_mode") is False

    def test_tool_choice_none_string_stays_text_protocol(self, monkeypatch) -> None:
        """``tool_choice="none"`` is the OpenAI way of disabling tools.
        Treat it as text-protocol (no native nudge) — and indeed the
        backend is unlikely to honour native tool_calls in that case.
        """
        captured = self._capture_native_mode(
            monkeypatch, tool_choice="none", tools=SAMPLE_TOOLS,
        )
        assert captured.get("native_mode") is False
