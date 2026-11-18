"""Change 1 — broadened triage fast-path.

A small local triage model rarely emits the exact token ``trivial``; it
says ``simple`` / ``low`` / ``easy`` / ``minor`` / ``Trivial.`` /
`` minor `` (padded). The fast-path gate must recognise those synonyms
(after normalising case/whitespace/punctuation) yet still route a
genuinely complex ticket off the fast path — never the reverse.

On the lean default graph every non-trivial ticket goes to the Planner
(``ROUTE_SMALL``); ``ROUTE_FULL`` (enhancer + context) is wired only under
``AIFORGE_FORCE_FULL_PIPELINE=1``.
"""
from __future__ import annotations

import asyncio

import pytest

from aiforge_core.runtime import graph_pipeline as gp


def _drive(result):
    """Drive a gate whether or not it is a coroutine — the graph_pipeline gates
    are sync node functions now (ADK awaits a node's result only when it is
    awaitable)."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


class _FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.route = None


# Where a non-trivial ticket goes on the lean default graph.
_NON_TRIVIAL = gp.ROUTE_SMALL


@pytest.fixture(autouse=True)
def _lean_graph(monkeypatch):
    monkeypatch.delenv("AIFORGE_FORCE_FULL_PIPELINE", raising=False)
    monkeypatch.delenv("AIFORGE_TRIAGE_STRICT", raising=False)


def _route(state: dict) -> str:
    ctx = _FakeCtx(state)
    _drive(gp._triage_gate(ctx))
    return ctx.route


@pytest.mark.parametrize("verdict", [
    "trivial", "simple", "low", "easy", "minor", "small",
    "Trivial.", " simple ", " minor ", "SIMPLE", "**easy**",
])
def test_trivial_synonyms_route_fast_path(verdict) -> None:
    assert _route({"complexity": verdict}) == gp.ROUTE_TRIVIAL


@pytest.mark.parametrize("verdict", [
    "moderate", "high", "complex", "hard", "medium", "difficult",
    "garbage", "", "unknown",
])
def test_nontrivial_routes_full(verdict) -> None:
    route = _route({"complexity": verdict})
    assert route != gp.ROUTE_TRIVIAL
    assert route == _NON_TRIVIAL


def test_triage_verdict_bare_string_synonym() -> None:
    # model emitted just the word, not JSON
    assert _route({"triage_verdict": "simple"}) == gp.ROUTE_TRIVIAL
    assert _route({"triage_verdict": " Easy. "}) == gp.ROUTE_TRIVIAL


def test_triage_verdict_bare_garbage_defaults_full() -> None:
    assert _route({"triage_verdict": "junk"}) == _NON_TRIVIAL


def test_triage_verdict_prose_wrapped_json() -> None:
    assert _route({"triage_verdict": 'Here is: {"complexity": "easy"}'}) \
        == gp.ROUTE_TRIVIAL


def test_strict_env_restores_exact_trivial(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_TRIAGE_STRICT", "1")
    assert _route({"complexity": "simple"}) == _NON_TRIVIAL
    assert _route({"complexity": "trivial"}) == gp.ROUTE_TRIVIAL


def test_force_full_takes_the_full_path(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_FORCE_FULL_PIPELINE", "1")
    assert _route({"complexity": "hard"}) == gp.ROUTE_FULL
