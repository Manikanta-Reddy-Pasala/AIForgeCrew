"""Tests for the research_entry passthrough fan-out source."""
from __future__ import annotations

import asyncio

from aiforge_core.runtime import parallel_stages as ps


class _Ctx:
    def __init__(self, state):
        self.state = state
        self.route = None


def test_research_entry_is_noop_passthrough() -> None:
    ctx = _Ctx({"enhanced_body": "x"})
    asyncio.run(ps.research_entry(ctx))
    assert ctx.state["enhanced_body"] == "x"   # state untouched
    assert ctx.route is None


def test_make_research_entry_node_named() -> None:
    n = ps.make_research_entry_node()
    assert n.name == "research_entry"
