"""Unit tests for the research-gap gate (runtime.graph_pipeline._gap_gate)
and its helpers. Pure-helper tests — no ADK import, so they run anywhere."""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import graph_pipeline as gp


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


def _run(coro):
    return asyncio.run(coro)


# ── _gap_sufficient (fail-open) ─────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ({"sufficient": True}, True),
    ({"sufficient": False}, False),
    ('{"sufficient": false}', False),
    ('{"sufficient": true}', True),
    ("not json at all", True),     # fail-open
    (None, True),                  # fail-open
    ({}, True),                    # no key → fail-open
])
def test_gap_sufficient(raw, expected) -> None:
    assert gp._gap_sufficient(raw) is expected


def test_render_gap_brief_lists_missing_and_queries() -> None:
    brief = gp._render_gap_brief(
        {"missing": ["token refresh path"], "queries": ["where is refresh"]})
    assert "token refresh path" in brief
    assert "where is refresh" in brief
    assert "INCOMPLETE" in brief


# ── _gap_gate routing ───────────────────────────────────────────────────

def test_gap_gate_routes_research_gap_when_insufficient() -> None:
    ctx = _FakeCtx({"gap_verdict": {"sufficient": False,
                                    "missing": ["token refresh path"],
                                    "queries": ["where is refresh"]}})
    _run(gp._gap_gate(ctx))
    assert ctx.route == gp.ROUTE_RESEARCH_GAP
    assert ctx.state["gap_pass_count"] == 1
    assert "token refresh path" in ctx.state["research_gap_brief_md"]


def test_gap_gate_routes_ok_when_sufficient() -> None:
    ctx = _FakeCtx({"gap_verdict": {"sufficient": True}})
    _run(gp._gap_gate(ctx))
    assert ctx.route == gp.ROUTE_RESEARCH_OK


def test_gap_gate_caps_at_one_pass() -> None:
    ctx = _FakeCtx({"gap_verdict": {"sufficient": False}, "gap_pass_count": 1})
    _run(gp._gap_gate(ctx))
    assert ctx.route == gp.ROUTE_RESEARCH_OK   # budget spent


def test_gap_gate_parse_failure_defaults_ok() -> None:
    ctx = _FakeCtx({"gap_verdict": "not json at all"})
    _run(gp._gap_gate(ctx))
    assert ctx.route == gp.ROUTE_RESEARCH_OK   # never block on a slip
