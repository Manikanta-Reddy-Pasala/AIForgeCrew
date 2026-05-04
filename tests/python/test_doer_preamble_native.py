"""Doer GA preamble — native-tool_calls mode (2026-05).

After ticket ONE-87 follow-up: the doer relies entirely on OpenAI
native ``tool_calls[]`` + ``tool_choice="required"``. The text-protocol
scaffolding that used to live inside the AIForge preamble (``<thinking>``,
``<summary>``, ``<tool_use>{...}</tool_use>``, "Interaction Protocol",
"End with a single ``<summary>`` tag") was friction — qwen3-coder /
gemma / glm-4 saw those instructions and either narrated their reasoning
in prose or invented custom XML tags like ``<file_content>...</file_content>``.

These tests pin the contract:

* ``_render_doer_preamble(worktree)`` and ``_build_user_input(...)``
  contain NO text-protocol markers.
* They DO contain the substantive job description: tool names exposed
  via the OpenAI ``tools`` array, the resolved compile_cmd, and the
  ``## Allowed files`` block.

The native-mode output channel (LM Studio + Ollama Cloud both verified)
is tested separately at ``test_ga_native_mode_prompt.py``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aiforge_core.runtime import repo_standards as rs


# Substrings that MUST NOT appear in the preamble or user_input — they
# are the text-protocol scaffolding the doer no longer uses.
_FORBIDDEN_MARKERS = (
    "<thinking>",
    "<summary>",
    "<tool_use>",
    "<tool_call>",
    "End with a single `<summary>",
    "End with a single <summary>",
    "Interaction Protocol",
    "interaction protocol",
)


# ─────────────────────────── _render_doer_preamble ──────────────────────


class TestRenderDoerPreambleNoTextProtocol:
    """The system prompt is pure native-tool_calls now."""

    def test_python_preamble_strips_text_protocol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer import ga_runner

        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        aiforge_dir = tmp_path / ".aiforge"
        aiforge_dir.mkdir()
        (aiforge_dir / "aiforge.conf.yml").write_text(
            "lang: python\ncompile_cmd: python -m compileall -q src\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))

        for marker in _FORBIDDEN_MARKERS:
            assert marker not in rendered, (
                f"text-protocol marker {marker!r} still in preamble:\n"
                f"{rendered}"
            )

    def test_java_preamble_strips_text_protocol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aiforge_core.doer import ga_runner

        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))

        for marker in _FORBIDDEN_MARKERS:
            assert marker not in rendered, (
                f"text-protocol marker {marker!r} still in preamble"
            )

    def test_preamble_keeps_substantive_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool names must still be advertised by name, the resolved
        compile_cmd must appear, and the allowed-files contract must
        be referenced. Strip the protocol — don't strip the job."""
        from aiforge_core.doer import ga_runner

        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        rendered = ga_runner._render_doer_preamble(str(tmp_path))

        # Tool names — model sees full schemas via OpenAI tools array,
        # but the preamble names them so the model knows what's wired.
        for tool in ("file_read", "file_write", "file_patch", "code_run",
                     "batch", "bulk_edit", "bash", "glob", "grep"):
            assert tool in rendered, f"tool name {tool!r} missing from preamble"

        # Compile cmd resolved from Standards.
        assert "mvn -q -DskipTests compile" in rendered

        # Allowed files contract.
        assert "## Allowed files" in rendered
        assert "ScopeGuard" in rendered or "blocked" in rendered

        # The new exit rule is documented.
        assert "tool_calls" in rendered or "stop calling tools" in rendered.lower()


# ─────────────────────────── _build_user_input ──────────────────────────


class TestBuildUserInputNoTextProtocol:
    """The per-ticket user message is also free of text-protocol scaffolding."""

    def _stub_ticket(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            identifier="TEST-1",
            title="Add `health` endpoint",
            body=(
                "Acceptance:\n"
                "- new GET /health returns 200\n"
                "- response body contains `{'status':'ok'}`\n"
            ),
        )

    def _build(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        """Render _build_user_input with all heavy context paths stubbed.

        Aider RepoMap, Neo4j neighbours, UnifiedContext, and conventions
        all hit external state that's flaky in unit-test mode. We stub
        each one to a deterministic empty/known value so the assertions
        focus on the protocol-stripping contract.
        """
        from aiforge_core.doer import ga_runner

        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
        monkeypatch.setattr(rs, "_from_neo4j", lambda name: None)
        monkeypatch.delenv("AIFORGE_COMPILE_CMD", raising=False)

        # Avoid expensive Aider RepoMap / Neo4j / UnifiedContext calls.
        monkeypatch.setattr(
            "aiforge_core.doer.ga_runner.aider_digest",
            lambda *a, **k: "",
        )
        monkeypatch.setattr(
            "aiforge_core.doer.ga_runner.graph_neighbours",
            lambda *a, **k: "",
        )

        # UnifiedContext lives at aiforge_core.context.UnifiedContext;
        # patch the constructor so .for_doer().render() returns "".
        try:
            import aiforge_core.context as _ctx_mod

            class _StubUC:
                def for_doer(self, ticket, token_budget=4500):
                    class _B:
                        def render(self): return ""
                    return _B()

            monkeypatch.setattr(_ctx_mod, "UnifiedContext", _StubUC)
        except Exception:
            pass

        ticket = self._stub_ticket()
        allowed = {
            str(tmp_path / "src/main/java/Health.java"),
            str(tmp_path / "src/main/java/HealthController.java"),
        }
        return ga_runner._build_user_input(
            ticket, "Plan: add /health endpoint", str(tmp_path), allowed,
        )

    def test_user_input_strips_text_protocol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rendered = self._build(tmp_path, monkeypatch)
        for marker in _FORBIDDEN_MARKERS:
            assert marker not in rendered, (
                f"text-protocol marker {marker!r} still in user_input:\n"
                f"{rendered}"
            )

    def test_user_input_keeps_substantive_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rendered = self._build(tmp_path, monkeypatch)

        # Allowed files block populated.
        assert "## Allowed files" in rendered
        assert "Health.java" in rendered
        assert "HealthController.java" in rendered

        # Acceptance criteria from ticket body still present.
        assert "Acceptance" in rendered or "/health" in rendered

        # Compile cmd referenced in workflow.
        assert "mvn -q -DskipTests compile" in rendered

        # Exit rule documented (zero tool_calls = end).
        assert "tool_calls" in rendered or "stop calling tools" in rendered.lower()
