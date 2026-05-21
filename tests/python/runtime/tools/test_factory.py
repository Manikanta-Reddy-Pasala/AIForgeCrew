from __future__ import annotations


def test_factory_returns_canonical_function_tools():
    from aiforge_core.runtime.tools import adk_function_tools
    tools = adk_function_tools()
    names = {t.func.__name__ for t in tools}
    # Sub #1: editor/bash/think/finish. Sub #2: browse.
    expected = {"editor", "bash", "browse", "think", "finish"}
    assert expected.issubset(names), f"missing tools: {expected - names}"


def test_factory_function_tools_are_adk_instances():
    from google.adk.tools import FunctionTool

    from aiforge_core.runtime.tools import adk_function_tools
    tools = adk_function_tools()
    for t in tools:
        assert isinstance(t, FunctionTool)
