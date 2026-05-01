"""Runtime — agent_runner + tool_registry."""
from __future__ import annotations

import aiforge_core.aiforge_agents.archetypes  # noqa: F401
from aiforge_core.aiforge_agents.runtime import agent_runner, tool_registry


def test_run_archetype_calls_run() -> None:
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
