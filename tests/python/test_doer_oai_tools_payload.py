"""Doer cfg + LLMSession ``tools`` / ``tool_choice`` plumbing (2026-05).

These tests pin the ONE-85 fix at three layers:

1. ``_doer_llm_config()`` returns a cfg with ``tool_choice`` set when
   the resolved provider is ``local`` or ``ollama_cloud``, and unset
   for ``anthropic`` (whose native tools wiring is upstream).
2. ``primary_cfg(tools=[...])`` threads tools through to the cfg
   passed to GA's ``LLMSession``.
3. The chat.completions payload built by GA's ``_openai_stream``
   includes ``tools`` and ``tool_choice`` when the session carries
   them, and excludes ``tool_choice`` when not set.

We don't hit the network: ``_payload_capture`` is a debug list we added
to ``_openai_stream`` that captures the payload before the HTTP POST.
The real call still fires (and fails fast on the bogus URL), but the
capture happens BEFORE that — sufficient for assertion.
"""
from __future__ import annotations

import os
import sys

import pytest


# ─── GA path bootstrap ────────────────────────────────────────────────


def _maybe_ga_path() -> str | None:
    for p in (
        os.environ.get("AIFORGE_GA_DIR"),
        "/Users/manip/genericagent",
        "/home/mani/genericagent",
    ):
        if p and os.path.isfile(os.path.join(p, "llmcore.py")):
            return p
    return None


_GA = _maybe_ga_path()
if _GA and _GA not in sys.path:
    sys.path.insert(0, _GA)

ga_required = pytest.mark.skipif(
    _GA is None,
    reason="GA llmcore.py not on host (set AIFORGE_GA_DIR)",
)


# ─── Layer 1: _doer_llm_config ────────────────────────────────────────


class TestDoerLlmConfig:
    """The cfg dict passed to GA's LLMSession."""

    def test_local_provider_gets_required_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aiforge_core.doer import ga_runner
        # Stub agent_config so the test is self-contained — we don't want
        # the live JSON file shape leaking in.
        from aiforge_core.runtime import agent_config as _acfg
        monkeypatch.setattr(_acfg, "get", lambda role: {
            "provider": "local", "model": "qwen3-coder:480b", "base_url": None,
        })
        monkeypatch.setattr(_acfg, "resolve_litellm", lambda role: {
            "model_id": "openai/qwen3-coder:480b",
            "api_base": "http://127.0.0.1:1234/v1",
            "api_key": "sk-local",
        })
        cfg = ga_runner._doer_llm_config()
        assert cfg["tool_choice"] == "required"
        # Model prefix stripped.
        assert cfg["model"] == "qwen3-coder:480b"

    def test_ollama_cloud_provider_gets_required_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aiforge_core.doer import ga_runner
        from aiforge_core.runtime import agent_config as _acfg
        monkeypatch.setattr(_acfg, "get", lambda role: {
            "provider": "ollama_cloud", "model": "llama3.1:70b", "base_url": None,
        })
        monkeypatch.setattr(_acfg, "resolve_litellm", lambda role: {
            "model_id": "ollama/llama3.1:70b",
            "api_base": "https://ollama.com/v1",
            "api_key": "k",
        })
        cfg = ga_runner._doer_llm_config()
        assert cfg["tool_choice"] == "required"

    def test_anthropic_provider_does_not_force_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Negative test: Anthropic runs through NativeClaudeSession with
        its own tools wiring; we MUST NOT force ``tool_choice=required``
        because that key has different semantics on Anthropic and would
        need a different request shape."""
        from aiforge_core.doer import ga_runner
        from aiforge_core.runtime import agent_config as _acfg
        monkeypatch.setattr(_acfg, "get", lambda role: {
            "provider": "anthropic", "model": "claude-sonnet-4", "base_url": None,
        })
        monkeypatch.setattr(_acfg, "resolve_litellm", lambda role: {
            "model_id": "anthropic/claude-sonnet-4",
            "api_base": "https://api.anthropic.com",
            "api_key": "sk-ant-...",
        })
        cfg = ga_runner._doer_llm_config()
        assert "tool_choice" not in cfg

    def test_env_override_wins(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aiforge_core.doer import ga_runner
        from aiforge_core.runtime import agent_config as _acfg
        monkeypatch.setattr(_acfg, "get", lambda role: {
            "provider": "local", "model": "x", "base_url": None,
        })
        monkeypatch.setattr(_acfg, "resolve_litellm", lambda role: {
            "model_id": "x", "api_base": None, "api_key": None,
        })
        monkeypatch.setenv("AIFORGE_DOER_TOOL_CHOICE", "auto")
        cfg = ga_runner._doer_llm_config()
        assert cfg["tool_choice"] == "auto"


# ─── Layer 2: primary_cfg(tools=...) ──────────────────────────────────


class TestPrimaryCfgTools:
    """``primary_cfg`` accepts a tools schema and threads it into cfg."""

    def test_tools_passed_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aiforge_core.doer.ga_tools import llm_config
        from aiforge_core.runtime import agent_config as _acfg
        from aiforge_core.runtime import llm_picker

        # llm_picker stub — return a non-gemini endpoint so we hit the
        # local-mlx branch.
        class _EP:
            backend = "lm_studio"
        monkeypatch.setattr(llm_picker, "pick", lambda role: _EP())
        monkeypatch.setattr(_acfg, "get", lambda role: {
            "provider": "local", "model": "qwen3", "base_url": None,
        })

        tools = [{"type": "function", "function": {"name": "file_read", "parameters": {}}}]
        cfg = llm_config.primary_cfg(tools=tools)
        assert cfg["tools"] == tools
        assert cfg["tool_choice"] == "required"

    def test_no_tools_no_tool_choice(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from aiforge_core.doer.ga_tools import llm_config
        from aiforge_core.runtime import llm_picker

        class _EP:
            backend = "lm_studio"
        monkeypatch.setattr(llm_picker, "pick", lambda role: _EP())
        cfg = llm_config.primary_cfg()  # tools=None
        assert "tools" not in cfg
        assert "tool_choice" not in cfg


# ─── Layer 3: GA _openai_stream payload (live capture, no network) ────


@ga_required
class TestOpenAiStreamPayload:
    """The chat.completions payload includes ``tools`` + ``tool_choice``
    when the LLMSession carries them. Captures payload before the
    HTTP POST via the ``_payload_capture`` debug hook we added to
    ``_openai_stream``."""

    def test_session_tools_appear_in_payload(self) -> None:
        import llmcore  # type: ignore
        captured: list[dict] = []
        cfg = {
            "name": "test",
            "apikey": "sk-test",
            "apibase": "http://127.0.0.1:0",  # unreachable port → fast fail
            "model": "qwen3-coder",
            "api_mode": "chat_completions",
            "stream": False,
            "max_retries": 0,
            "connect_timeout": 1,
            "read_timeout": 1,
            "tools": [{"type": "function", "function": {"name": "file_read"}}],
            "tool_choice": "required",
        }
        sess = llmcore.LLMSession(cfg=cfg)
        sess._payload_capture = captured  # type: ignore[attr-defined]
        # Drive the generator. We expect a network error, but the
        # payload was captured BEFORE the POST attempt.
        gen = sess.raw_ask([{"role": "user", "content": "hi"}])
        try:
            list(gen)
        except Exception:
            pass
        assert captured, "payload should have been captured before HTTP attempt"
        payload = captured[0]
        assert payload.get("tools") and payload["tools"][0]["function"]["name"] == "file_read"
        assert payload.get("tool_choice") == "required"
        # Sanity — model + messages still there.
        assert payload["model"] == "qwen3-coder"
        assert payload["messages"][-1]["content"] == "hi"

    def test_session_without_tools_omits_tool_choice(self) -> None:
        import llmcore  # type: ignore
        captured: list[dict] = []
        cfg = {
            "name": "test",
            "apikey": "sk-test",
            "apibase": "http://127.0.0.1:0",
            "model": "claude-sonnet-4",
            "api_mode": "chat_completions",
            "stream": False,
            "max_retries": 0,
            "connect_timeout": 1,
            "read_timeout": 1,
        }
        sess = llmcore.LLMSession(cfg=cfg)
        sess._payload_capture = captured  # type: ignore[attr-defined]
        try:
            list(sess.raw_ask([{"role": "user", "content": "hi"}]))
        except Exception:
            pass
        assert captured
        payload = captured[0]
        assert "tool_choice" not in payload
        assert "tools" not in payload

    def test_response_format_forwarded_when_set(self) -> None:
        import llmcore  # type: ignore
        captured: list[dict] = []
        cfg = {
            "name": "test", "apikey": "k", "apibase": "http://127.0.0.1:0",
            "model": "qwen3", "api_mode": "chat_completions",
            "stream": False, "max_retries": 0,
            "connect_timeout": 1, "read_timeout": 1,
            "response_format": {"type": "json_object"},
        }
        sess = llmcore.LLMSession(cfg=cfg)
        sess._payload_capture = captured  # type: ignore[attr-defined]
        try:
            list(sess.raw_ask([{"role": "user", "content": "x"}]))
        except Exception:
            pass
        assert captured
        assert captured[0].get("response_format") == {"type": "json_object"}
