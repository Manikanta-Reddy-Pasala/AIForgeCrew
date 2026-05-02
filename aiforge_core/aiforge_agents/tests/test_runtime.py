"""Runtime — agent_runner + tool_registry."""
from __future__ import annotations

import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents.runtime import agent_runner, tool_registry


def test_run_archetype_calls_run() -> None:
    from unittest.mock import patch
    fake = {
        "problem": "test", "knowns": [], "unknowns": [],
        "risks": [], "ambiguities": [],
    }
    with patch(
        "aiforge_core.aiforge_agents.runtime.llm_client.call_json",
        return_value=fake,
    ), patch(
        "aiforge_core.aiforge_agents.memory.code_context.query",
        return_value="ctx_md",
    ):
        out = agent_runner.run_archetype("understander", ctx={})
    assert out["artifact_type"] == "understanding"


def test_tool_registry_register_get() -> None:
    @tool_registry.register("test_echo")
    def _echo(text: str) -> str:
        return text
    assert "test_echo" in tool_registry.known()
    assert tool_registry.call("test_echo", text="ping") == "ping"


def test_tool_double_register_raises() -> None:
    import pytest

    @tool_registry.register("double_register_test")
    def _a() -> int:
        return 1

    with pytest.raises(ValueError):
        @tool_registry.register("double_register_test")
        def _b() -> int:
            return 2


# ─────────── Resilient JSON parse (local-model output) ────────────

def test_resilient_json_parse_strict() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    assert _resilient_json_parse('{"a": 1}') == {"a": 1}


def test_resilient_json_parse_strips_3_backticks() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    raw = '```json\n{"a": 1}\n```'
    assert _resilient_json_parse(raw) == {"a": 1}


def test_resilient_json_parse_strips_4_backticks() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    raw = '````json\n{"a": 1}\n````'
    assert _resilient_json_parse(raw) == {"a": 1}


def test_resilient_json_parse_fence_with_prose() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    raw = 'Sure, here is the answer:\n```json\n{"a": 1}\n```\nHope that helps!'
    assert _resilient_json_parse(raw) == {"a": 1}


def test_resilient_json_parse_no_fence_with_prose() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    raw = 'My answer: {"a": 1, "b": "two"}. That is all.'
    assert _resilient_json_parse(raw) == {"a": 1, "b": "two"}


def test_resilient_json_parse_returns_none_on_garbage() -> None:
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    assert _resilient_json_parse("not json at all") is None
    assert _resilient_json_parse("") is None
    assert _resilient_json_parse(None) is None


def test_resilient_json_parse_rejects_non_object() -> None:
    """Lists / scalars are rejected — archetypes always expect an object."""
    from aiforge_core.aiforge_agents.runtime.llm_client import (
        _resilient_json_parse,
    )
    assert _resilient_json_parse('[1, 2, 3]') is None
    assert _resilient_json_parse('"a string"') is None
    assert _resilient_json_parse('42') is None
